"""Cover what a batch does through the CLI: plan, one confirmation, the checksum policy, grouping by mode, cancelling, cleanup and failures.

Behaviour covered: an update of two user and two system packages makes one plan call, asks one confirmation and makes exactly two ``update`` calls (the user call first, then one call holding
all system targets); from a gupkg that cannot elevate the system targets are reported as needing the system gupkg and no call carries them; the policy decides per package and the relayed
``--skip-checksum`` argument reaches only the call of its group; a declined confirmation, a refused target, a failed plan and a hostile name leave the right targets unrun and
the rest running; the combined exit code is the highest failure (a refusal counts 3, a skip 5); cancelling after the first call starts no second call and reports its targets skipped; with
``[cleanup] auto = true`` an update ends with a cleanup of each updated root; ``check-updates`` is one call and offers a package refused for a missing checksum once.

Real: the flows, the session, the follower and real subprocesses. Mocked: the ``gupkg`` executable (a scripted double) and the machine's settings files (temporary).
Out of scope: the screens (``test_ui_screens.py``) and the real CLI (``test_manager_cli.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gupkg_manager.batch import check_all, check_updates_all, pick, update_all
from gupkg_ui.interaction import Interaction
from gupkg_ui.plans import PackageRow, read_packages
from gupkg_ui.session import BatchOutcome, BatchSession
from tests.ui_support import FakeGupkg, ListSink, until, entry, list_doc, make_env, plan_doc, plan_entry, plan_failure, result


def rows_for(base: Path, *packages: tuple[str, str]) -> list[PackageRow]:
    """Return outdated inventory rows for ``(name, mode)`` pairs, with package folders under the test's roots."""
    roots = base / "roots"
    document = list_doc([(name, mode, "outdated", "1.0.0", "2.0.0", str(roots / mode / name)) for name, mode in packages])
    return read_packages(document)


def logs_of(base: Path, mode: str) -> str:
    """Return the log folder of a mode's root, where the double writes its events file."""
    return str(base / "roots" / mode / ".gupkg" / "logs")


def schedule_run(fake: FakeGupkg, base: Path, mode: str, names: list[str], *, doc_entries=None, relaunched=False, **extra) -> None:
    """Schedule the next ``update`` call of the double: it succeeds for *names* in the root of *mode*."""
    entries = doc_entries if doc_entries is not None else [entry(name, mode=mode) for name in names]
    child = result("update", entries)
    parent = result("update", [], relaunched=True) if relaunched else child
    events = {"dir": logs_of(base, mode), "elevated": relaunched, "result": child, "packages": [{"name": name, "path": str(base / "roots" / mode / name), "records": []} for name in names], **extra}
    fake.respond("update", doc=parent, events=events)


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


def test_two_user_and_two_system_targets_make_two_calls_one_plan_and_one_confirmation(fake: FakeGupkg, tmp_path: Path) -> None:
    """The acceptance case of design 13.2: one plan, one question, the user call first, then ONE call that holds both system targets."""
    env = make_env(tmp_path, fake, can_elevate=True)
    rows = rows_for(tmp_path, ("a", "user"), ("b", "user"), ("c", "system"), ("d", "system"))
    fake.respond("update:plan", doc=plan_doc([
        plan_entry("a", "a"), plan_entry("b", "b"), plan_entry("c", "c", mode="system"), plan_entry("d", "d", mode="system"),
    ]))
    schedule_run(fake, tmp_path, "user", ["a", "b"])
    schedule_run(fake, tmp_path, "system", ["c", "d"], relaunched=True)
    questions: list[str] = []
    interaction = Interaction(confirm=lambda lines, question: questions.append(question) or True, interactive=True)

    outcome, sink = run_flow(update(env, rows, interaction), env)

    assert len(questions) == 1 and "2 user and 2 system packages" in questions[0] and "one administrator prompt" in questions[0]
    calls = update_calls(fake)
    assert [call["targets"] for call in calls] == [["a", "b"], ["c", "d"]]
    assert len([call for call in fake.calls() if call["key"] == "update:plan"]) == 1 and len(fake.calls()) == 3
    assert outcome.ok and sorted(item["package"] for item in outcome.entries) == ["a", "b", "c", "d"]
    verdict = next(index for index, line in enumerate(sink.shown) if line.startswith("━━ Done"))
    assert [line.strip().split(":")[0] for line in sink.shown[verdict + 1:]] == ["ok a 2.0.0", "ok b 2.0.0", "ok c 2.0.0", "ok d 2.0.0"]


def test_from_a_gupkg_that_cannot_elevate_no_system_target_is_run(fake: FakeGupkg, tmp_path: Path) -> None:
    """A user-writable gupkg asks for no administrator rights: the user targets run, the system targets are reported as needing the system gupkg, exit code 2."""
    env = make_env(tmp_path, fake, can_elevate=False)
    rows = rows_for(tmp_path, ("a", "user"), ("c", "system"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("c", "c", mode="system")]))
    schedule_run(fake, tmp_path, "user", ["a"])

    outcome, sink = run_flow(update(env, rows, Interaction()), env)

    assert [call["targets"] for call in update_calls(fake)] == [["a"]]
    refused = next(item for item in outcome.entries if item["package"] == "c")
    assert refused["status"] == "needs-system-gupkg" and refused["exit_code"] == 2 and outcome.exit_code == 2
    assert any("needs the system gupkg" in line for line in sink.shown) and any("Start gupkg-manager from the system gupkg" in line for line in sink.shown)


def test_a_declined_confirmation_runs_nothing(fake: FakeGupkg, tmp_path: Path) -> None:
    """No: nothing is called but the plan; every target is reported as cancelled and the exit code is 5."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("a", "user"), ("b", "user"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b")]))

    outcome, _ = run_flow(update(env, rows, Interaction(confirm=lambda lines, question: False, interactive=True)), env)

    assert update_calls(fake) == [] and outcome.exit_code == 5
    assert {item["status"] for item in outcome.entries} == {"cancelled"}


def test_the_confirmation_may_be_a_coroutine(fake: FakeGupkg, tmp_path: Path) -> None:
    """A dialog answers asynchronously; the flow waits for it."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("a", "user"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a")]))
    schedule_run(fake, tmp_path, "user", ["a"])

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
    rows = rows_for(tmp_path, ("hashed", "user"), ("bare", "user"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("hashed", "hashed", sha256="present"), plan_entry("bare", "bare", sha256="missing")]))
    schedule_run(fake, tmp_path, "user", ["hashed", "bare"] if runs else ["hashed"])
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
    rows = rows_for(tmp_path, ("ok", "user"), ("bad", "user"), ("plain", "user"))
    fake.respond("update:plan", doc=plan_doc([
        plan_entry("ok", "ok", sha256="missing"), plan_failure("bad", "invalid-checksum", "sha256 is malformed", package="bad"),
        plan_entry("plain", "plain", sha256="missing", url="http://example.org/p.zip"),
    ]))
    schedule_run(fake, tmp_path, "user", ["ok"])

    outcome, sink = run_flow(update(env, rows, Interaction()), env)

    (call,) = update_calls(fake)
    assert call["targets"] == ["ok"] and "--skip-checksum=setting" in call["options"]
    statuses = {item["package"]: item["status"] for item in outcome.entries}
    assert statuses["bad"] == "refused" and statuses["plain"] == "refused" and outcome.exit_code == 3
    assert any("malformed" in line for line in sink.shown) and any("not https" in line for line in sink.shown)


def test_a_plan_that_cannot_be_read_fails_every_target_and_runs_nothing(fake: FakeGupkg, tmp_path: Path) -> None:
    """A plan call that prints garbage is a failed batch (exit 3), never an empty successful one."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("a", "user"), ("b", "user"))
    fake.respond("update:plan", raw="oops", exit=0)

    outcome, _ = run_flow(update(env, rows, Interaction()), env)

    assert update_calls(fake) == [] and outcome.exit_code == 3
    assert all("plan could not be made" in item["error"]["message"] for item in outcome.entries) and len(outcome.entries) == 2


def test_a_hostile_package_name_fails_alone_and_the_others_run(fake: FakeGupkg, tmp_path: Path) -> None:
    """A name with a character the launcher could misread is refused before any call; it never blocks the rest."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("good", "user"), ("evil&calc", "user"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("good", "good")]))
    schedule_run(fake, tmp_path, "user", ["good"])

    outcome, _ = run_flow(update(env, rows, Interaction()), env)

    assert [call["targets"] for call in update_calls(fake)] == [["good"]]
    evil = next(item for item in outcome.entries if item["package"] == "evil&calc")
    assert not evil["ok"] and "cannot be passed to gupkg safely" in evil["error"]["message"] and outcome.exit_code == 2


def test_one_failed_package_does_not_hide_the_others(fake: FakeGupkg, tmp_path: Path) -> None:
    """The batch continues past a failure and reports the highest exit code of the targets."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("a", "user"), ("b", "user"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b")]))
    schedule_run(fake, tmp_path, "user", ["a", "b"], doc_entries=[entry("a"), entry("b", ok=False, status="failed", code=3, error={"code": "x", "message": "disk full"})])

    outcome, sink = run_flow(update(env, rows, Interaction()), env)

    assert outcome.exit_code == 3 and [item["ok"] for item in outcome.entries] == [True, False]
    assert any("x b: update failed: disk full" in line for line in sink.shown)


def test_a_call_that_returns_no_entry_for_a_target_fails_that_target(fake: FakeGupkg, tmp_path: Path) -> None:
    """A target the call never answered for is failed in the report, never assumed to have worked."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("a", "user"), ("b", "user"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b")]))
    schedule_run(fake, tmp_path, "user", ["a"], doc_entries=[entry("a")])

    outcome, _ = run_flow(update(env, rows, Interaction()), env)

    lost = next(item for item in outcome.entries if item["package"] == "b")
    assert not lost["ok"] and outcome.exit_code == 4 and "no result for it" in lost["error"]["message"]


def test_cancelling_during_the_first_call_starts_no_second_call_and_skips_its_targets(fake: FakeGupkg, tmp_path: Path) -> None:
    """The stop marker is created while the user call waits after package one; package two is skipped by the CLI, the system call never starts and its targets are skipped."""
    env = make_env(tmp_path, fake, can_elevate=True)
    rows = rows_for(tmp_path, ("a", "user"), ("b", "user"), ("c", "system"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("b", "b"), plan_entry("c", "c", mode="system")]))
    stopped = result("update", [entry("a"), entry("b", status="skipped", version=None, previous=None, code=5)])
    fake.respond("update", doc=stopped, events={
        "dir": logs_of(tmp_path, "user"), "stop_after": 0, "result": stopped,
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
    assert by_name == {"a": "updated", "b": "skipped", "c": "skipped"} and outcome.cancelled and outcome.exit_code == 5


@pytest.mark.parametrize("where", ["settings-option", "user-layer"])
def test_the_system_policy_ignores_a_value_an_elevated_run_would_not_trust(fake: FakeGupkg, tmp_path: Path, where: str) -> None:
    """A ``skip`` from ``--settings`` or from the user's own settings file never lets an administrator-run system install skip a missing checksum; a user target still follows it."""
    skip = '[manager]\nchecksum_missing = "skip"'
    env = make_env(tmp_path, fake, settings=skip if where == "settings-option" else "", user_layer=skip if where == "user-layer" else "")
    rows = rows_for(tmp_path, ("c", "system"), ("u", "user"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("c", "c", mode="system", sha256="missing"), plan_entry("u", "u", sha256="missing")]))
    schedule_run(fake, tmp_path, "user", ["u"])

    outcome, _ = run_flow(update(env, rows, Interaction()), env)

    (call,) = update_calls(fake)
    assert call["targets"] == ["u"] and "--skip-checksum=setting" in call["options"]
    assert {item["package"]: item["status"] for item in outcome.entries} == {"c": "refused", "u": "updated"}


def test_cleanup_ends_an_update_only_when_the_setting_asks_for_it(fake: FakeGupkg, tmp_path: Path) -> None:
    """With ``[cleanup] auto = true`` the update ends with a cleanup of the user root; without it nothing is cleaned and old versions stay."""
    for auto, expected in ((True, 1), (False, 0)):
        base = tmp_path / f"auto-{auto}"
        base.mkdir()
        double = FakeGupkg(base)
        env = make_env(base, double, settings=f"[cleanup]\nauto = {str(auto).lower()}")
        rows = rows_for(base, ("a", "user"))
        double.respond("update:plan", doc=plan_doc([plan_entry("a", "a")]))
        schedule_run(double, base, "user", ["a"])
        double.respond("cleanup", doc=result("cleanup", [entry("a", "cleanup", status="cleaned", version=None, previous=None)]))

        run_flow(update(env, rows, Interaction()), env)

        cleanups = [call for call in double.calls() if call["key"] == "cleanup"]
        assert len(cleanups) == expected
        if expected:
            assert cleanups[0]["options"][:1] == ["--yes"] and "--mode" not in cleanups[0]["options"]


def test_the_cleanup_after_an_update_is_one_call_for_the_one_scope(fake: FakeGupkg, tmp_path: Path) -> None:
    """Updating packages with auto cleanup ends with one cleanup call: a run has one scope, so there is no cleanup per mode."""
    env = make_env(tmp_path, fake, settings="[cleanup]\nauto = true")
    rows = rows_for(tmp_path, ("a", "user"), ("c", "system"))
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a"), plan_entry("c", "c", mode="system")]))
    schedule_run(fake, tmp_path, "user", ["a"])
    schedule_run(fake, tmp_path, "system", ["c"], relaunched=True)
    fake.respond("cleanup", doc=result("cleanup", []))

    run_flow(update(env, rows, Interaction()), env)

    cleanups = [call for call in fake.calls() if call["key"] == "cleanup"]
    assert len(cleanups) == 1 and "--mode" not in cleanups[0]["options"]


def test_check_updates_is_one_call_and_activates_nothing(fake: FakeGupkg, tmp_path: Path) -> None:
    """A check is a single call carrying every target (the CLI never elevates for it); no update call is made."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("a", "user"), ("c", "system"))
    doc = result("check-updates", [entry("a", "check-updates", status="written"), entry("c", "check-updates", status="reported", mode="system")])
    fake.respond("check-updates", doc=doc, events={"dir": logs_of(tmp_path, "user"), "result": doc, "packages": [{"name": "a", "path": str(tmp_path / "roots" / "user" / "a"), "records": []}]})

    outcome, _ = run_flow(lambda session, sink: check_updates_all(env, rows, session=session, interaction=Interaction()), env)

    (call,) = fake.calls()
    assert call["key"] == "check-updates" and call["targets"] == ["a", "c"] and outcome.ok


def test_a_version_refused_for_its_missing_checksum_is_offered_once_and_rerun_when_approved(fake: FakeGupkg, tmp_path: Path) -> None:
    """After a check the package whose download has no checksum is asked about once (policy ask, someone present); a yes re-runs only it with the user's approval."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("a", "user"), ("b", "user"))
    refusal = {"code": "checksum-missing", "message": "no checksum", "url": "https://example.org/b.zip", "skippable": True}
    first = result("check-updates", [entry("a", "check-updates", status="written"), entry("b", "check-updates", ok=False, status="failed", code=3, error=refusal)])
    again = result("check-updates", [entry("b", "check-updates", status="written")])
    for doc, names in ((first, ["a", "b"]), (again, ["b"])):
        fake.respond("check-updates", doc=doc, events={"dir": logs_of(tmp_path, "user"), "result": doc, "packages": [{"name": name, "path": str(tmp_path / "roots" / "user" / name), "records": []} for name in names]})
    asked: list[str] = []

    outcome, _ = run_flow(lambda session, sink: check_updates_all(env, rows, session=session, interaction=Interaction(ask_checksum=lambda item: asked.append(item.target) or True, interactive=True)), env)

    first_call, second_call = fake.calls()
    assert asked == ["b"] and second_call["targets"] == ["b"] and "--skip-checksum=user-approved" in second_call["options"]
    assert "--skip-checksum=user-approved" not in first_call["options"] and outcome.ok


def test_check_with_fix_asks_the_cli_for_one_grouped_call(fake: FakeGupkg, tmp_path: Path) -> None:
    """The health check without ``--fix`` is one non-elevated call for every package; ``--fix`` groups system packages into the one elevated call."""
    env = make_env(tmp_path, fake)
    rows = rows_for(tmp_path, ("a", "user"), ("c", "system"))
    for name, mode, status in (("a", "user", "healthy"), ("c", "system", "fixed")):
        doc = result("check", [entry(name, "check", status=status, mode=mode)])
        fake.respond("check", doc=doc, events={"dir": logs_of(tmp_path, mode), "result": doc, "packages": [{"name": name, "path": str(tmp_path / "roots" / mode / name), "records": []}]})

    run_flow(lambda session, sink: check_all(env, rows, session=session), env)
    plain_calls = fake.calls()
    run_flow(lambda session, sink: check_all(env, rows, session=session, fix=True), env)
    fixing = fake.calls()[len(plain_calls):]

    assert [call["targets"] for call in plain_calls] == [["a", "c"]]
    assert [call["targets"] for call in fixing] == [["a"], ["c"]] and all("--fix" in call["options"] for call in fixing)


def test_pick_chooses_what_each_all_batch_acts_on() -> None:
    """``update --all`` takes the outdated packages, ``check-updates --all`` the installed and local ones, and a mode filter narrows both."""
    rows = read_packages(list_doc([
        ("a", "user", "outdated", "1", "2", None), ("b", "user", "installed", "1", None, None), ("c", "system", "outdated", "1", "2", None),
        ("d", "user", "local", None, "1", None), ("e", "user", "available", None, None, None), ("f", "user", "broken", "1", None, None),
    ]))

    assert [row.name for row in pick(rows, "update")] == ["a", "c"]
    assert [row.name for row in pick(rows, "update", mode="user")] == ["a"]
    assert [row.name for row in pick(rows, "check-updates")] == ["a", "b", "c", "d", "f"]
    assert [row.name for row in pick(rows, "check")] == ["a", "b", "c", "d", "f"]
