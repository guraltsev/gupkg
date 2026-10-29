"""Cover what a batch does through the CLI: plan, one confirmation, the checksum policy, one call per batch, cancelling, cleanup and failures.

Behaviour covered: an update of three packages makes one plan call, asks one confirmation and makes exactly one ``update`` call holding all three targets; the policy decides per package and the relayed
``--skip-checksum`` argument reaches the call; a declined confirmation, a refused target, a failed plan and a hostile name leave the right targets unrun and
the rest running; the combined exit code is the highest failure (a refusal counts 3, a skip 5); cancelling reports the targets of a call that never started skipped; with
``[cleanup] auto = true`` an update ends with one cleanup of the root; ``check-updates`` is one call and offers a package refused for a missing checksum once.

Real: the flows, the session, the follower and real subprocesses. Mocked: the ``gupkg`` executable (a scripted double) and the machine's settings files (temporary).
Out of scope: the screens (``test_ui_screens.py``) and the real CLI (``test_manager_cli.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gupkg_manager.batch import check_all, check_updates_all, pick, update_all
from gupkg_ui.interaction import Interaction
from gupkg_ui.plans import PackageRow
from gupkg_ui.session import BatchOutcome, BatchSession
from tests.ui_support import FakeGupkg, ListSink, until, entry, make_env, plan_doc, plan_entry, plan_failure, result


def rows_for(base: Path, *names: str) -> list[PackageRow]:
    """Return outdated inventory rows for *names*, with package folders under the test's root."""
    return [PackageRow(name, "outdated", "1.0.0", "2.0.0", str(base / "roots" / "user" / name), None) for name in names]


def logs_of(base: Path) -> str:
    """Return the log folder of the root, where the double writes its events file."""
    return str(base / "roots" / "user" / ".gupkg" / "logs")


def schedule_run(fake: FakeGupkg, base: Path, names: list[str], *, doc_entries=None, **extra) -> None:
    """Schedule the next ``update`` call of the double: it succeeds for *names* in the root."""
    entries = doc_entries if doc_entries is not None else [entry(name) for name in names]
    child = result("update", entries)
    events = {"dir": logs_of(base), "result": child, "packages": [{"name": name, "path": str(base / "roots" / "user" / name), "records": []} for name in names], **extra}
    fake.respond("update", doc=child, events=events)


def run_flow(coroutine_factory, env) -> tuple[BatchOutcome, ListSink]:
    """Run ``coroutine_factory(session, sink)`` to the end with a recording sink."""
    sink = ListSink()
    session = BatchSession(env.client, sink, temp_dir=None)
    return asyncio.run(coroutine_factory(session, sink)), sink


def update(env, rows, interaction: Interaction):
    """Return the coroutine factory of an update of *rows*."""
    return lambda session, sink: update_all(env, rows, session=session, sink=sink, interaction=interaction)


def update_calls(fake: FakeGupkg) -> list[dict]:
    """Return the non-plan ``update`` calls the double received."""
    return [call for call in fake.calls() if call["key"] == "update"]


@pytest.fixture
def fake(tmp_path: Path) -> FakeGupkg:
    """A scripted ``gupkg`` double."""
    return FakeGupkg(tmp_path)


def test_an_update_of_three_packages_makes_one_plan_one_confirmation_and_one_call_with_all_three_targets(fake: FakeGupkg, tmp_path: Path) -> None:
    """One plan, one question and ONE ``update`` call that holds all three targets (the CLI acts in the run's own scope)."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "b", "c")
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b"), plan_entry("c", "c")]))
    schedule_run(fake, tmp_path, ["a", "b", "c"])
    questions: list[str] = []
    interaction = Interaction(confirm=lambda lines, question: questions.append(question) or True, interactive=True)

    outcome, sink = run_flow(update(env, rows, interaction), env)

    assert questions == ["Update 3 packages?"]
    assert [call["targets"] for call in update_calls(fake)] == [["a", "b", "c"]]
    assert len([call for call in fake.calls() if call["key"] == "update:plan"]) == 1 and len(fake.calls()) == 2
    assert outcome.ok and sorted(item["package"] for item in outcome.entries) == ["a", "b", "c"]
    verdict = next(index for index, line in enumerate(sink.shown) if line.startswith("━━ Done"))
    assert [line.strip().split(":")[0] for line in sink.shown[verdict + 1:]] == ["ok a 2.0.0", "ok b 2.0.0", "ok c 2.0.0"]


def test_a_declined_confirmation_runs_nothing(fake: FakeGupkg, tmp_path: Path) -> None:
    """No: nothing is called but the plan; every target is reported as cancelled and the exit code is 5."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "b")
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b")]))

    outcome, _ = run_flow(update(env, rows, Interaction(confirm=lambda lines, question: False, interactive=True)), env)

    assert update_calls(fake) == [] and outcome.exit_code == 5
    assert {item["status"] for item in outcome.entries} == {"cancelled"}


def test_the_confirmation_may_be_a_coroutine(fake: FakeGupkg, tmp_path: Path) -> None:
    """A dialog answers asynchronously; the flow waits for it."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a")
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a")]))
    schedule_run(fake, tmp_path, ["a"])

    async def yes(lines, question):
        """Answer after yielding to the loop."""
        await asyncio.sleep(0.01)
        return True

    outcome, _ = run_flow(update(env, rows, Interaction(confirm=yes, interactive=True)), env)

    assert outcome.ok and len(update_calls(fake)) == 1


@pytest.mark.parametrize(
    ("policy", "interactive", "answer", "relays", "runs"),
    [
        pytest.param("ask", True, True, "--skip-checksum=user-approved", True, id="ask-yes"),
        pytest.param("ask", True, False, None, False, id="ask-no"),
        pytest.param("ask", False, True, None, False, id="ask-nobody-is-refuse"),
        pytest.param("refuse", True, True, None, False, id="refuse"),
        pytest.param("skip", False, True, "--skip-checksum=setting", True, id="skip"),
    ],
)
def test_the_policy_decides_a_package_without_a_checksum_and_relays_only_where_needed(fake: FakeGupkg, tmp_path: Path, policy, interactive, answer, relays, runs) -> None:
    """The missing-hash package is run with the relayed flag, or left out; the package with a hash runs in the same call either way."""
    env = make_env(tmp_path, fake, settings=f'[manager]\nchecksum_missing = "{policy}"')
    rows = rows_for(tmp_path, "hashed", "bare")
    fake.respond("update:plan", doc=plan_doc([plan_entry("hashed", "hashed", sha256="present"), plan_entry("bare", "bare", sha256="missing")]))
    schedule_run(fake, tmp_path, ["hashed", "bare"] if runs else ["hashed"])
    asked: list[str] = []
    interaction = Interaction(ask_checksum=lambda item: asked.append(item.target) or answer, interactive=interactive)

    outcome, _ = run_flow(update(env, rows, interaction), env)

    (call,) = update_calls(fake)
    assert ("bare" in call["targets"]) is runs and "hashed" in call["targets"]
    assert ([option for option in call["options"] if option.startswith("--skip-checksum")] == [relays]) if relays else not any(option.startswith("--skip-checksum") for option in call["options"])
    assert (asked == ["bare"]) is (policy == "ask" and interactive)
    bare = next((item for item in outcome.entries if item["package"] == "bare"), None)
    assert (bare is not None and bare["status"] in ("cancelled", "refused")) is (not runs)
    assert outcome.exit_code == (0 if runs else (5 if answer is False and interactive else 3))


def test_a_malformed_hash_and_an_http_url_are_refused_under_the_skip_policy(fake: FakeGupkg, tmp_path: Path) -> None:
    """Even ``skip`` never runs a package whose hash is malformed or whose URL is plain http; the others still run."""
    env = make_env(tmp_path, fake, settings='[manager]\nchecksum_missing = "skip"')
    rows = rows_for(tmp_path, "ok", "bad", "plain")
    fake.respond("update:plan", doc=plan_doc([
        plan_entry("ok", "ok", sha256="missing"), plan_failure("bad", "invalid-checksum", "sha256 is malformed", package="bad"),
        plan_entry("plain", "plain", sha256="missing", url="http://example.org/p.zip"),
    ]))
    schedule_run(fake, tmp_path, ["ok"])

    outcome, sink = run_flow(update(env, rows, Interaction()), env)

    (call,) = update_calls(fake)
    assert call["targets"] == ["ok"] and "--skip-checksum=setting" in call["options"]
    statuses = {item["package"]: item["status"] for item in outcome.entries}
    assert statuses["bad"] == "refused" and statuses["plain"] == "refused" and outcome.exit_code == 3
    assert any("malformed" in line for line in sink.shown) and any("not https" in line for line in sink.shown)


def test_a_plan_that_cannot_be_read_fails_every_target_and_runs_nothing(fake: FakeGupkg, tmp_path: Path) -> None:
    """A plan call that prints garbage is a failed batch (exit 3), never an empty successful one."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "b")
    fake.respond("update:plan", raw="oops", exit=0)

    outcome, _ = run_flow(update(env, rows, Interaction()), env)

    assert update_calls(fake) == [] and outcome.exit_code == 3
    assert all("plan could not be made" in item["error"]["message"] for item in outcome.entries) and len(outcome.entries) == 2


def test_a_hostile_package_name_fails_alone_and_the_others_run(fake: FakeGupkg, tmp_path: Path) -> None:
    """A name with a character the launcher could misread is refused before any call; it never blocks the rest."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "good", "evil&calc")
    fake.respond("update:plan", doc=plan_doc([plan_entry("good", "good")]))
    schedule_run(fake, tmp_path, ["good"])

    outcome, _ = run_flow(update(env, rows, Interaction()), env)

    assert [call["targets"] for call in update_calls(fake)] == [["good"]]
    evil = next(item for item in outcome.entries if item["package"] == "evil&calc")
    assert not evil["ok"] and "cannot be passed to gupkg safely" in evil["error"]["message"] and outcome.exit_code == 2


def test_one_failed_package_does_not_hide_the_others(fake: FakeGupkg, tmp_path: Path) -> None:
    """The batch continues past a failure and reports the highest exit code of the targets."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "b")
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b")]))
    schedule_run(fake, tmp_path, ["a", "b"], doc_entries=[entry("a"), entry("b", ok=False, status="failed", code=3, error={"code": "x", "message": "disk full"})])

    outcome, sink = run_flow(update(env, rows, Interaction()), env)

    assert outcome.exit_code == 3 and [item["ok"] for item in outcome.entries] == [True, False]
    assert any("x b: update failed: disk full" in line for line in sink.shown)


def test_a_call_that_returns_no_entry_for_a_target_fails_that_target(fake: FakeGupkg, tmp_path: Path) -> None:
    """A target the call never answered for is failed in the report, never assumed to have worked."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "b")
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b")]))
    schedule_run(fake, tmp_path, ["a"], doc_entries=[entry("a")])

    outcome, _ = run_flow(update(env, rows, Interaction()), env)

    lost = next(item for item in outcome.entries if item["package"] == "b")
    assert not lost["ok"] and outcome.exit_code == 4 and "no result for it" in lost["error"]["message"]


def test_cancelling_during_the_first_call_starts_no_second_call_and_skips_its_targets(fake: FakeGupkg, tmp_path: Path) -> None:
    """The stop marker is created while the call waits after package one; package two is skipped by the CLI and the follow-up call never starts."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "b")
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b")]))
    stopped = result("update", [entry("a"), entry("b", status="skipped", version=None, previous=None, code=5)])
    fake.respond("update", doc=stopped, events={
        "dir": logs_of(tmp_path), "stop_after": 0, "result": stopped,
        "packages": [{"name": name, "path": str(tmp_path / "roots" / "user" / name), "records": []} for name in ("a", "b")],
    })

    async def scenario():
        """Run the update, cancel once package a has ended in the log, and return the outcome."""
        sink = ListSink()
        session = BatchSession(env.client, sink)
        task = asyncio.ensure_future(update_all(env, rows, session=session, sink=sink, interaction=Interaction()))
        await until(lambda: any(line.startswith("Package a: ") for line in sink.shown), "package a to end")
        session.request_cancel()
        return await task

    outcome = asyncio.run(scenario())

    assert [call["targets"] for call in update_calls(fake)] == [["a", "b"]] and fake.stop_events()[0]["saw_stop"]
    by_name = {item["package"]: item["status"] for item in outcome.entries}
    assert by_name == {"a": "updated", "b": "skipped"} and outcome.cancelled and outcome.exit_code == 5


def test_cleanup_ends_an_update_only_when_the_setting_asks_for_it(fake: FakeGupkg, tmp_path: Path) -> None:
    """With ``[cleanup] auto = true`` the update ends with a cleanup of the user root; without it nothing is cleaned and old versions stay."""
    for auto, expected in ((True, 1), (False, 0)):
        base = tmp_path / f"auto-{auto}"
        base.mkdir()
        double = FakeGupkg(base)
        env = make_env(base, double, settings=f"[cleanup]\nauto = {str(auto).lower()}")
        rows = rows_for(base, "a")
        double.respond("update:plan", doc=plan_doc([plan_entry("a", "a")]))
        schedule_run(double, base, ["a"])
        double.respond("cleanup", doc=result("cleanup", [entry("a", "cleanup", status="cleaned", version=None, previous=None)]))

        run_flow(update(env, rows, Interaction()), env)

        cleanups = [call for call in double.calls() if call["key"] == "cleanup"]
        assert len(cleanups) == expected
        if expected:
            assert cleanups[0]["options"][:1] == ["--yes"]


def test_the_cleanup_after_an_update_is_one_call_for_the_one_scope(fake: FakeGupkg, tmp_path: Path) -> None:
    """Updating packages with auto cleanup ends with one cleanup call for the run's root."""
    env = make_env(tmp_path, fake, settings="[cleanup]\nauto = true")
    rows = rows_for(tmp_path, "a", "c")
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("c", "c")]))
    schedule_run(fake, tmp_path, ["a", "c"])
    fake.respond("cleanup", doc=result("cleanup", []))

    run_flow(update(env, rows, Interaction()), env)

    cleanups = [call for call in fake.calls() if call["key"] == "cleanup"]
    assert len(cleanups) == 1


def test_check_updates_is_one_call_and_activates_nothing(fake: FakeGupkg, tmp_path: Path) -> None:
    """A check is a single call carrying every target; no update call is made."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "c")
    doc = result("check-updates", [entry("a", "check-updates", status="written"), entry("c", "check-updates", status="reported")])
    fake.respond("check-updates", doc=doc, events={"dir": logs_of(tmp_path), "result": doc, "packages": [{"name": "a", "path": str(tmp_path / "roots" / "user" / "a"), "records": []}]})

    outcome, _ = run_flow(lambda session, sink: check_updates_all(env, rows, session=session, interaction=Interaction()), env)

    (call,) = fake.calls()
    assert call["key"] == "check-updates" and call["targets"] == ["a", "c"] and outcome.ok


def test_a_version_refused_for_its_missing_checksum_is_offered_once_and_rerun_when_approved(fake: FakeGupkg, tmp_path: Path) -> None:
    """After a check the package whose download has no checksum is asked about once (policy ask, someone present); a yes re-runs only it with the user's approval."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "b")
    refusal = {"code": "checksum-missing", "message": "no checksum", "url": "https://example.org/b.zip", "skippable": True}
    first = result("check-updates", [entry("a", "check-updates", status="written"), entry("b", "check-updates", ok=False, status="failed", code=3, error=refusal)])
    again = result("check-updates", [entry("b", "check-updates", status="written")])
    for doc, names in ((first, ["a", "b"]), (again, ["b"])):
        fake.respond("check-updates", doc=doc, events={"dir": logs_of(tmp_path), "result": doc, "packages": [{"name": name, "path": str(tmp_path / "roots" / "user" / name), "records": []} for name in names]})
    asked: list[str] = []

    outcome, _ = run_flow(lambda session, sink: check_updates_all(env, rows, session=session, interaction=Interaction(ask_checksum=lambda item: asked.append(item.target) or True, interactive=True)), env)

    first_call, second_call = fake.calls()
    assert asked == ["b"] and second_call["targets"] == ["b"] and "--skip-checksum=user-approved" in second_call["options"]
    assert "--skip-checksum=user-approved" not in first_call["options"] and outcome.ok


def test_check_with_and_without_fix_is_one_call_for_every_package(fake: FakeGupkg, tmp_path: Path) -> None:
    """The health check is one call for every package, with ``--fix`` only when asked."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, "a", "c")
    doc = result("check", [entry("a", "check", status="healthy"), entry("c", "check", status="fixed")])
    fake.respond("check", doc=doc, events={"dir": logs_of(tmp_path), "result": doc, "packages": [
        {"name": name, "path": str(tmp_path / "roots" / "user" / name), "records": []} for name in ("a", "c")]})

    run_flow(lambda session, sink: check_all(env, rows, session=session), env)
    plain_calls = fake.calls()
    run_flow(lambda session, sink: check_all(env, rows, session=session, fix=True), env)
    fixing = fake.calls()[len(plain_calls):]

    assert [call["targets"] for call in plain_calls] == [["a", "c"]] and "--fix" not in plain_calls[0]["options"]
    assert [call["targets"] for call in fixing] == [["a", "c"]] and "--fix" in fixing[0]["options"]


def test_pick_chooses_what_each_all_batch_acts_on() -> None:
    """``update --all`` takes the outdated packages and ``check-updates --all`` the installed, local and broken ones."""
    rows = [
        PackageRow(name, status, version, newest, None, None)
        for name, status, version, newest in (
            ("a", "outdated", "1", "2"), ("b", "installed", "1", None), ("c", "outdated", "1", "2"),
            ("d", "local", None, "1"), ("e", "available", None, None), ("f", "broken", "1", None),
        )
    ]

    assert [row.name for row in pick(rows, "update")] == ["a", "c"]
    assert [row.name for row in pick(rows, "check-updates")] == ["a", "b", "c", "d", "f"]
    assert [row.name for row in pick(rows, "check")] == ["a", "b", "c", "d", "f"]
