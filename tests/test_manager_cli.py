"""Cover the non-interactive commands of ``gupkg-manager`` and the exit codes that schedules rely on.

Behaviour covered: ``update --all --yes`` plans, runs and exits 0; ``update --all`` with nobody to ask (no terminal, no ``--yes``, or ``--json``) changes nothing and exits 5; at a terminal
the one question is asked and a no changes nothing (5); a checksum-less package under the default ``ask`` policy in a ``--yes`` run is refused (exit 3) while the others run;
``--json`` prints exactly one document with the CLI's head and the human text goes to standard error; a failing target gives the CLI's code; ``check-updates --all`` and ``check`` run as one
call each; ``--all`` is required; an elevated run from a gupkg tree that standard users can change, an unreadable package list and a missing terminal for the browser are refused with their own exit codes.

Real: the command line of ``gupkg-manager``, the batch flows and real subprocesses. Mocked: the ``gupkg`` executable (a scripted double), the machine (``FakeSystem``) and the console streams.
Out of scope: the real CLI behind the real launcher (``test_manager_real_cli.py``) and the screens.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from gupkg.system.fake import FakeSystem
from gupkg_manager import cli
from tests.test_ui_flows import logs_of, schedule_run
from tests.ui_support import FakeGupkg, entry, list_doc, make_env, plan_doc, plan_entry, result


class Terminal(io.StringIO):
    """A stream that claims to be a console and holds what a person types."""

    def isatty(self) -> bool:
        """Report a console."""
        return True


@pytest.fixture
def fake(tmp_path: Path) -> FakeGupkg:
    """A scripted ``gupkg`` double."""
    return FakeGupkg(tmp_path)


def manager(tmp_path: Path, fake: FakeGupkg, *argv: str, typed: str | None = None, elevated: bool = False, settings: str = ""):
    """Run ``gupkg-manager`` with the double and return ``(exit code, standard output, standard error)``."""
    env = make_env(tmp_path, fake, settings=settings)
    source = Terminal(typed) if typed is not None else io.StringIO("")
    out, err = (Terminal(), Terminal()) if typed is not None else (io.StringIO(), io.StringIO())
    environment = {**fake.client().env, **env.env}
    code = cli.main(
        ["--settings", str(tmp_path / "ui-settings.toml"), *argv], location=env.location, system=FakeSystem(tmp_path / "machine", elevated=elevated),
        env=environment, command=fake.client().command, stdin=source, stdout=out, stderr=err, reads=fake.reads(),
    )
    return code, out.getvalue(), err.getvalue()


def schedule_update_all(fake: FakeGupkg, tmp_path: Path, *, missing: bool = False) -> None:
    """Schedule an inventory with two outdated user packages (one without a checksum when *missing*), its plan and its run."""
    fake.respond("list", doc=list_doc([(name, "user", "outdated", "1.0.0", "2.0.0", str(tmp_path / "roots" / "user" / name)) for name in ("a", "b")]))
    fake.respond("update:plan", doc=plan_doc([plan_entry("a", "a", sha256="present"), plan_entry("b", "b", sha256="missing" if missing else "present")]))
    schedule_run(fake, tmp_path, ["a"] if missing else ["a", "b"])


def test_update_all_with_yes_runs_and_exits_zero(fake: FakeGupkg, tmp_path: Path) -> None:
    """The schedule form: no question, the plan and the run are printed, exit 0, and the call carries ``--yes`` for the CLI's own confirmation."""
    schedule_update_all(fake, tmp_path)

    code, out, err = manager(tmp_path, fake, "update", "--all", "--yes")

    assert code == 0 and "Plan" in out and "ok a 2.0.0: updated" in out and out.rstrip().splitlines()[-1].startswith("    ok b")
    (call,) = [item for item in fake.calls() if item["key"] == "update"]
    assert call["targets"] == ["a", "b"] and "--yes" in call["options"] and "--no-input" in call["options"]


def test_update_all_without_yes_and_without_a_terminal_changes_nothing(fake: FakeGupkg, tmp_path: Path) -> None:
    """Nobody can confirm: the plan is shown, nothing runs, exit code 5 (input needed), and the output says to pass ``--yes``."""
    schedule_update_all(fake, tmp_path)

    code, out, err = manager(tmp_path, fake, "update", "--all")

    assert code == 5 and "Pass --yes" in out and [item for item in fake.calls() if item["key"] == "update"] == []


@pytest.mark.parametrize(("typed", "code", "runs"), [pytest.param("y\n", 0, True, id="yes"), pytest.param("n\n", 5, False, id="no"), pytest.param("", 5, False, id="closed-input")])
def test_at_a_terminal_the_plan_is_asked_once(fake: FakeGupkg, tmp_path: Path, typed: str, code: int, runs: bool) -> None:
    """A person gets one question for the whole plan; yes runs it, anything else leaves everything unchanged."""
    schedule_update_all(fake, tmp_path)

    result_code, out, err = manager(tmp_path, fake, "update", "--all", typed=typed)

    assert result_code == code and out.count("[y/N]") == 1
    assert bool([item for item in fake.calls() if item["key"] == "update"]) is runs


def test_a_schedule_with_ask_refuses_the_package_without_a_checksum_and_runs_the_rest(fake: FakeGupkg, tmp_path: Path) -> None:
    """The default policy ``ask`` with nobody to answer is ``refuse``: exit 3, the refusal is named, the other package is updated."""
    schedule_update_all(fake, tmp_path, missing=True)

    code, out, err = manager(tmp_path, fake, "update", "--all", "--yes")

    assert code == 3 and "refused: no checksum" in out and "ok a 2.0.0: updated" in out
    (call,) = [item for item in fake.calls() if item["key"] == "update"]
    assert call["targets"] == ["a"]


def test_json_prints_one_document_and_the_human_text_goes_to_standard_error(fake: FakeGupkg, tmp_path: Path) -> None:
    """A script reads standard output as one document with the CLI's head, one entry per target and the calls that were made."""
    schedule_update_all(fake, tmp_path)

    code, out, err = manager(tmp_path, fake, "update", "--all", "--yes", "--json")

    document = json.loads(out)
    assert code == 0 and (document["api"], document["command"], document["ok"], document["exit_code"]) == (2, "update", True, 0)
    assert [item["package"] for item in document["results"]] == ["a", "b"] and len(document["calls"]) == 1
    assert "Plan" in err and "━━ Done" in err


def test_json_without_yes_never_asks_even_at_a_terminal(fake: FakeGupkg, tmp_path: Path) -> None:
    """``--json`` is for scripts: it asks nothing, so a plan without ``--yes`` is exit code 5 with a document that says so."""
    schedule_update_all(fake, tmp_path)

    code, out, err = manager(tmp_path, fake, "update", "--all", "--json", typed="y\n")

    assert code == 5 and json.loads(out)["exit_code"] == 5 and [item for item in fake.calls() if item["key"] == "update"] == []


def test_a_failed_target_gives_the_cli_exit_code(fake: FakeGupkg, tmp_path: Path) -> None:
    """The highest failure code of the targets is the exit code."""
    schedule_update_all(fake, tmp_path)
    fake.scenario["update"].clear()
    schedule_run(fake, tmp_path, ["a", "b"], doc_entries=[entry("a"), entry("b", ok=False, status="failed", code=3, error={"code": "x", "message": "disk full"})])

    code, out, _ = manager(tmp_path, fake, "update", "--all", "--yes")

    assert code == 3 and "x b: update failed: disk full" in out


def test_nothing_to_update_is_success(fake: FakeGupkg, tmp_path: Path) -> None:
    """With no outdated package the command says so and exits 0 without planning anything."""
    fake.respond("list", doc=list_doc([("a", "user", "installed", "1.0.0", None, None)]))

    code, out, _ = manager(tmp_path, fake, "update", "--all", "--yes")

    assert code == 0 and "No packages to update" in out and fake.read_calls() == ["list"] and fake.calls() == []


def test_check_updates_all_is_one_call_without_questions(fake: FakeGupkg, tmp_path: Path) -> None:
    """``check-updates --all`` needs neither ``--yes`` nor a terminal: one call carries every installed or local package and activates nothing."""
    fake.respond("list", doc=list_doc([("a", "user", "installed", "1.0.0", None, str(tmp_path / "roots" / "user" / "a")), ("b", "user", "local", None, "1.0.0", str(tmp_path / "roots" / "user" / "b"))]))
    doc = result("check-updates", [entry("a", "check-updates", status="written"), entry("b", "check-updates", status="written")])
    fake.respond("check-updates", doc=doc, events={"dir": logs_of(tmp_path), "result": doc, "packages": [{"name": "a", "path": str(tmp_path / "roots" / "user" / "a"), "records": []}]})

    code, out, _ = manager(tmp_path, fake, "check-updates", "--all")

    (call,) = [item for item in fake.calls() if item["key"] == "check-updates"]
    assert code == 0 and call["targets"] == ["a", "b"] and not [item for item in fake.calls() if item["key"] == "update"]


def test_check_is_one_read_only_call(fake: FakeGupkg, tmp_path: Path) -> None:
    """The health check lists every package and asks the CLI once; without ``--fix`` it changes nothing and needs no confirmation."""
    fake.respond("list", doc=list_doc([("a", "user", "installed", "1.0.0", None, str(tmp_path / "roots" / "user" / "a"))]))
    doc = result("check", [entry("a", "check", status="healthy")])
    fake.respond("check", doc=doc, events={"dir": logs_of(tmp_path), "result": doc, "packages": [{"name": "a", "path": str(tmp_path / "roots" / "user" / "a"), "records": []}]})

    code, out, _ = manager(tmp_path, fake, "check")

    (call,) = [item for item in fake.calls() if item["key"] == "check"]
    assert code == 0 and "--fix" not in call["options"] and "ok a 2.0.0: healthy" in out


def test_check_with_fix_needs_a_yes(fake: FakeGupkg, tmp_path: Path) -> None:
    """Repairing is a change: without ``--yes`` and without a terminal it is refused (exit 5) and nothing is called but the list."""
    fake.respond("list", doc=list_doc([("a", "user", "installed", "1.0.0", None, str(tmp_path / "roots" / "user" / "a"))]))

    code, out, _ = manager(tmp_path, fake, "check", "--fix")

    assert code == 5 and fake.read_calls() == ["list"] and fake.calls() == []


@pytest.mark.parametrize("command", ["update", "check-updates"])
def test_all_is_required(fake: FakeGupkg, tmp_path: Path, command: str) -> None:
    """The batch commands act on every package only when ``--all`` says so; named packages belong to ``gupkg``."""
    code, _, err = manager(tmp_path, fake, command)

    assert code == 2 and "needs --all" in err and fake.calls() == []


def test_an_elevated_run_from_a_gupkg_tree_that_standard_users_can_change_is_refused(fake: FakeGupkg, tmp_path: Path) -> None:
    """The one refusal left: an elevated run trusts only an admin-only gupkg tree (exit 2, before any call); the elevated shell itself is no reason."""
    code, _, err = manager(tmp_path, fake, "update", "--all", "--yes", elevated=True)

    assert code == 2 and "admin-only" in err and fake.calls() == []


def test_an_unreadable_package_list_is_a_failure(fake: FakeGupkg, tmp_path: Path) -> None:
    """If the listing cannot be read (a document the reader refuses) the command fails with exit 3 and says why; nothing is planned."""
    fake.respond("list", doc={"garbage": True})

    code, _, err = manager(tmp_path, fake, "update", "--all", "--yes")

    assert code == 3 and "package list could not be read" in err


def test_the_browser_needs_a_terminal(fake: FakeGupkg, tmp_path: Path) -> None:
    """Without a terminal the browser does not start (exit 2) and points to the batch commands."""
    code, _, err = manager(tmp_path, fake)

    assert code == 2 and "needs a terminal" in err


def test_the_help_names_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` lists the browser and the three batch commands."""
    assert cli.main(["--help"]) == 0

    text = capsys.readouterr().out
    assert all(word in text for word in ("tui", "check-updates", "update", "check"))
