"""Pin the public, versioned machine interface of the command line (``"api": 1``): documents, events file, exit codes.

Behaviour covered: the result document of a lifecycle call (its keys and the keys of each target's entry); the plan document
of ``--plan --json`` for every command that accepts ``--plan`` (version, mode, elevation, ``sha256: present | missing |
invalid``, URL, effects) and that a plan changes nothing on disk except the run's log files; the error document of a usage
error; the events file (JSON lines, flushed per event, ending with ``run_end`` that carries the result document and
announcing the run's stop marker in ``run_start``); a stop marker created during a run stops it after the package that is
running, and a file that already exists changes nothing; ``--events-file`` with a path is a usage error that writes nothing;
the exit codes 0, 2, 3, 4, 5 and 6; human output goes to standard error under ``--json``.

Real: the argument parser, the engine, the run session and its log files, real folders. Mocked: ``FakeSystem`` and the
environment mapping built by the test.
Out of scope: the content of each event (``test_event_contract.py``) and the elevated relaunch (``test_cli_elevation.py``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gupkg.engine import lifecycle
from tests.cli_support import CliWorld
from tests.engine_support import ScriptDouble
from tests.mode_support import pin_no_ambient_elevation, tree_snapshot  # noqa: F401

RESULT_KEYS = {"api", "command", "ok", "exit_code", "relaunched", "results", "warnings"}
ENTRY_KEYS = {"package", "operation", "ok", "status", "version", "previous", "mode", "exit_code", "receipt", "log", "warnings"}
# A successful entry also carries ``details``, a failed one ``error``; plans add keys that belong to their operation only.
SUCCESS_ENTRY_KEYS = ENTRY_KEYS | {"details"}
FAILED_ENTRY_KEYS = ENTRY_KEYS | {"error"}
ERROR_KEYS = {"code", "message"}  # plus "hint", present only when there is one


def assert_error_shape(error: dict) -> None:
    """Assert the pinned shape of one structured error: ``code`` and ``message``, and ``hint`` only when it has text."""
    assert set(error) in (ERROR_KEYS, ERROR_KEYS | {"hint"}) and error["code"] and error["message"]
    assert "hint" not in error or (isinstance(error["hint"], str) and error["hint"])
PLAN_EXTRA: dict[str, set[str]] = {
    "check-updates": {"download", "writes"}, "install": {"populate"}, "update": {"populate"}, "switch": {"populate"},
    "uninstall": set(), "cleanup": {"cleanup"},
}
PLAN_KEYS = {
    "operation", "package", "path", "mode", "requires_elevation", "version", "previous", "effects", "phases", "notes", "sha256", "url",
}


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A fake machine with tool 1.0.0 installed in the user root and 2.0.0 published beside it."""
    machine = CliWorld(tmp_path)
    machine.publish("tool", "1.0.0")
    assert machine.cli("install", "tool").code == 0
    machine.publish("tool", "2.0.0")
    return machine


def test_the_result_document_has_the_pinned_keys(world: CliWorld) -> None:
    """One document on standard output: the call's head, then one entry per target with the pinned keys."""
    result = world.cli("update", "tool", "--json")

    document = result.document()
    assert set(document) == RESULT_KEYS and document["api"] == 1 and document["command"] == "update"
    assert document["ok"] is True and document["exit_code"] == 0 and document["relaunched"] is False
    (entry,) = document["results"]
    assert set(entry) == SUCCESS_ENTRY_KEYS and entry["package"] == "tool" and entry["status"] == "updated"
    assert (entry["version"], entry["previous"], entry["mode"]) == ("2.0.0", "1.0.0", "user")
    assert Path(entry["receipt"]).is_file() and Path(entry["log"]).is_file()


def test_human_output_goes_to_standard_error_when_json_is_asked(world: CliWorld) -> None:
    """Standard output holds exactly the document; the phases and the summary are on standard error."""
    result = world.cli("update", "tool", "--json")

    json.loads(result.out)
    assert "==> Fetch" in result.err and "tool 2.0.0: updated" in result.err


@pytest.mark.parametrize(
    ("argv", "operation"),
    [
        pytest.param(("check-updates", "tool"), "check-updates", id="check-updates"),
        pytest.param(("install", "tool"), "install", id="install"),
        pytest.param(("update", "tool"), "update", id="update"),
        pytest.param(("uninstall", "tool"), "uninstall", id="uninstall"),
        pytest.param(("switch", "tool", "1.0.0"), "switch", id="switch"),
        pytest.param(("cleanup", "tool"), "cleanup", id="cleanup"),
    ],
)
def test_every_command_that_accepts_plan_prints_a_plan_document_and_changes_nothing(
    world: CliWorld, argv: tuple[str, ...], operation: str
) -> None:
    """--plan --json names the target's version, mode, elevation need, checksum status and URL, and writes only the log files."""
    world.tracker("tool")
    world.announce("tool", "2.0.0")
    roots = [world.user, world.system_root]
    before = [tree_snapshot(root) for root in roots]
    machine_before = (len(world.fake.registry_log), world.fake.broadcasts, sorted(str(path) for path in world.fake.shortcuts()))

    result = world.cli(*argv, "--plan", "--json")

    document = result.document()
    assert result.code == 0, result.err
    assert document["plan"] is True and document["command"] == operation and document["ok"] is True
    entry = document["plans"][0]
    assert entry["target"] == "tool" and entry["ok"] is True
    assert set(entry) == {"target", "ok", "exit_code", "plan"} and set(entry["plan"]) == PLAN_KEYS | PLAN_EXTRA.get(operation, set()) and entry["plan"]["operation"] == operation
    assert entry["plan"]["mode"] == "user" and entry["plan"]["requires_elevation"] is False
    after = [tree_snapshot(root) for root in roots]
    for old, new in zip(before, after):
        changed = {name for name in set(old) | set(new) if old.get(name) != new.get(name)}
        assert all(".gupkg\\logs" in name or name.endswith(".gupkg") or name.endswith("logs") for name in changed), changed
    assert (len(world.fake.registry_log), world.fake.broadcasts, sorted(str(path) for path in world.fake.shortcuts())) == machine_before


@pytest.mark.parametrize(
    ("hashed", "status", "skippable"),
    [
        pytest.param(True, "present", False, id="declared"),
        pytest.param(False, "missing", True, id="absent-and-skippable"),
    ],
)
def test_the_plan_marks_the_checksum_present_or_missing(world: CliWorld, hashed: bool, status: str, skippable: bool) -> None:
    """The plan of an install says whether the download's SHA-256 is declared or absent, and whether --skip-checksum could allow it."""
    world.publish("fresh", "1.0.0", hashed=hashed)

    result = world.cli("install", "fresh", "--plan", "--json")

    plan = result.document()["plans"][0]["plan"]
    assert plan["sha256"] == status and plan["url"].endswith("fresh-1.0.0.zip")
    assert plan["populate"]["checksum"]["sha256"] == status and plan["populate"]["checksum"]["skippable"] is skippable


def test_a_malformed_hash_is_never_planned_and_never_skippable(world: CliWorld) -> None:
    """A declared hash that is not 64 hexadecimal digits makes the version invalid: no plan, and --skip-checksum does not help."""
    folder = world.publish("fresh", "1.0.0", hashed=True)
    text = folder.joinpath("pkg.toml").read_text(encoding="utf-8")
    start = text.index('sha256 = "') + len('sha256 = "')
    folder.joinpath("pkg.toml").write_text(text[:start] + "not-a-hash" + text[start + 64:], encoding="utf-8")

    planned = world.cli("install", "fresh", "--plan", "--json")
    skipped = world.cli("install", "fresh", "--skip-checksum", "--json")

    error = planned.document()["plans"][0]["result"]["error"]
    assert planned.code == 2 and error["code"] == "invalid-checksum"
    assert set(error) == {"code", "message", "version", "field"} and (error["version"], error["field"]) == ("1.0.0", "source.sha256")
    assert skipped.code == 2 and not (world.user / "fresh" / "current").exists()
    assert skipped.document()["results"][0]["error"]["code"] == "invalid-checksum" and "64 hexadecimal" in planned.out


def test_a_named_malformed_version_has_the_same_stable_code(world: CliWorld) -> None:
    """`name@version` of a version whose declared hash is malformed is `invalid-checksum` too, with the version and the field."""
    folder = world.publish("fresh", "1.0.0", hashed=True)
    text = folder.joinpath("pkg.toml").read_text(encoding="utf-8")
    start = text.index('sha256 = "') + len('sha256 = "')
    folder.joinpath("pkg.toml").write_text(text[:start] + "zz" + text[start + 2:], encoding="utf-8")

    result = world.cli("install", "fresh@1.0.0", "--json")

    error = result.document()["results"][0]["error"]
    assert result.code == 2 and error["code"] == "invalid-checksum" and (error["version"], error["field"]) == ("1.0.0", "source.sha256")


def test_a_bare_install_that_falls_back_to_an_older_version_names_the_invalid_newer_one(world: CliWorld) -> None:
    """When the newest version directory is invalid, `install tool` installs the older valid one and says which one it skipped."""
    world.publish("fresh", "1.0.0")
    newer = world.publish("fresh", "2.0.0")
    text = newer.joinpath("pkg.toml").read_text(encoding="utf-8")
    start = text.index('sha256 = "') + len('sha256 = "')
    newer.joinpath("pkg.toml").write_text(text[:start] + "zz" + text[start + 2:], encoding="utf-8")

    refused = world.cli("install", "fresh", "--json")

    shown = refused.out + refused.err
    assert refused.code == 5 and "other version directories that are broken or not supported: 2.0.0" in shown and "Install 1.0.0 anyway?" in shown

    result = world.cli("install", "fresh", "--allow-other-broken-versions", "--json")

    assert result.code == 0, result.err
    assert result.document()["results"][0]["version"] == "1.0.0"
    assert "fresh 2.0.0 is newer than 1.0.0 but not valid" in result.err and "sha256 is malformed" in result.err


def test_a_plan_that_cannot_be_made_is_the_targets_failure_with_its_exit_code(world: CliWorld) -> None:
    """An unknown target is an entry with ok false and its structured error; the document's exit code is the highest one."""
    result = world.cli("install", "nothing-like-this", "--plan", "--json")

    document = result.document()
    assert result.code == 2 and document["ok"] is False and document["exit_code"] == 2
    entry = document["plans"][0]
    assert entry["ok"] is False and entry["result"]["error"]["code"] == "not-found"
    assert set(entry) == {"target", "ok", "exit_code", "result"} and set(entry["result"]) == FAILED_ENTRY_KEYS
    assert_error_shape(entry["result"]["error"])
    assert set(document) == {"api", "command", "ok", "exit_code", "plan", "plans"}


def test_a_failed_entry_and_an_error_document_have_exactly_the_pinned_keys(world: CliWorld) -> None:
    """A failed target has the base keys plus ``error`` (code, message, hint); a document-level error has errors[] and nothing else."""
    failed = world.cli("install", "nothing-like-this", "--json").document()
    usage = world.cli("install", "tool", "--mode", "nowhere", "--json").document()
    declined = world.cli("uninstall", "tool", "--purge", "--no-input", "--json").document()

    assert set(failed) == RESULT_KEYS and set(failed["results"][0]) == FAILED_ENTRY_KEYS
    assert_error_shape(failed["results"][0]["error"])
    assert set(usage) == {"api", "command", "ok", "exit_code", "errors"}
    assert_error_shape(usage["errors"][0])
    assert set(declined) == RESULT_KEYS and set(declined["results"][0]) == FAILED_ENTRY_KEYS
    assert_error_shape(declined["results"][0]["error"])


def test_a_usage_error_is_exit_2_with_an_error_document_when_json_is_asked(world: CliWorld) -> None:
    """A refused command line prints the message on standard error and, with --json, the error document on standard output."""
    result = world.cli("install", "tool", "--mode", "nowhere", "--json")

    document = result.document()
    assert result.code == 2 and "invalid choice" in result.err
    assert (document["api"], document["ok"], document["exit_code"]) == (1, False, 2)
    assert document["errors"][0]["code"] == "usage"


def test_the_events_file_is_json_lines_that_end_with_the_result_document(world: CliWorld) -> None:
    """Every line is a JSON event, run_start announces the stop marker, and run_end carries the same result as --json."""
    result = world.cli("update", "tool", "--events-file", "run.jsonl", "--json")

    lines = (world.user / ".gupkg" / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["event"] == "run_start" and records[0]["api"] == 1 and records[0]["stop_file"]
    assert Path(records[0]["stop_file"]).parent == world.temp and not Path(records[0]["stop_file"]).exists()
    assert records[-1]["event"] == "run_end" and records[-1]["result"] == result.document()
    package_lines = (world.user / "tool" / ".gupkg" / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["event"] == "phase_start" for line in package_lines)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("..\\x.jsonl", id="parent-backslash"), pytest.param("sub/x.jsonl", id="subfolder"),
        pytest.param("C:x.jsonl", id="drive-relative"), pytest.param("..", id="dot-dot"), pytest.param("x:y", id="stream"),
        pytest.param("con.jsonl", id="device-name"), pytest.param("a b.jsonl", id="space"), pytest.param("x.log", id="text-log-name"),
    ],
)
def test_an_events_file_that_is_not_a_plain_name_is_a_usage_error_and_writes_nothing(world: CliWorld, name: str) -> None:
    """A path, a drive, a stream, a device name or a text-log name is refused before any work: exit 2 and no new file anywhere."""
    before = tree_snapshot(world.user)

    result = world.cli("update", "tool", "--events-file", name)

    assert result.code == 2 and "events file name" in result.err
    assert tree_snapshot(world.user) == before and current(world) == "1.0.0"


def current(world: CliWorld) -> str:
    """Return the version that the user package ``tool`` has active."""
    return Path(os.path.realpath(world.user / "tool" / "current")).name


class StopDouble(ScriptDouble):
    """A populate script that, like a user pressing Esc in the manager, creates the stop marker the run announced."""

    def __init__(self, files: dict[str, str], logs: Path) -> None:
        """Remember the folder where the run writes its logs."""
        super().__init__(files)
        self.logs = logs

    def __call__(self, call):
        """Read the announced marker from the run's events and create it, then fill App as usual."""
        newest = sorted(self.logs.glob("*.jsonl"))[-1]
        first = json.loads(newest.read_text(encoding="utf-8").splitlines()[0])
        assert first["event"] == "run_start" and first["stop_file"]
        Path(first["stop_file"]).write_text("stop", encoding="utf-8")
        return super().__call__(call)


def test_a_stop_marker_created_during_the_run_stops_it_after_the_current_package(world: CliWorld) -> None:
    """The announced marker makes the run finish package one, skip the rest and exit 5; the marker is removed at the end."""
    double = world.publish_script("one", "1.0.0")
    world.fake.processes["populate.cmd"] = StopDouble({"app.exe": "one"}, world.user / ".gupkg" / "logs")
    world.publish("two", "1.0.0")
    world.publish("three", "1.0.0")

    result = world.cli("install", "one", "two", "three", "--yes", "--json")

    statuses = [item["status"] for item in result.document()["results"]]
    assert result.code == 5 and statuses == ["installed", "skipped", "skipped"]
    assert not (world.user / "two" / "current").exists() and not list(world.temp.glob("gupkg-stop-*"))
    assert double is not None


def test_a_file_that_already_exists_is_not_a_stop_request(world: CliWorld) -> None:
    """The marker's name is chosen when the run starts, so a file planted beforehand (any name) never skips a package."""
    (world.temp / "gupkg-stop-1234").write_text("stop", encoding="utf-8")
    world.publish("one", "1.0.0")
    world.publish("two", "1.0.0")

    result = world.cli("install", "one", "two", "--yes")

    assert result.code == 0 and (world.user / "two" / "current").is_dir()


def test_two_runs_announce_different_stop_markers(world: CliWorld) -> None:
    """Each run gets a fresh marker name, so a stale request cannot stop the next run."""
    first = world.cli("update", "tool", "--events-file", "a.jsonl")
    second = world.cli("check", "tool", "--events-file", "b.jsonl")
    names = [
        json.loads((world.user / ".gupkg" / "logs" / name).read_text(encoding="utf-8").splitlines()[0])["stop_file"]
        for name in ("a.jsonl", "b.jsonl")
    ]

    assert first.code == second.code == 0 and names[0] != names[1]


def test_exit_code_6_is_a_package_that_needs_a_newer_gupkg(world: CliWorld) -> None:
    """A package that requires a newer gupkg, or has a pkg.toml format this gupkg does not know, exits with code 6."""
    world.publish("future", "1.0.0", top='requires_gupkg = ">=99.0"')
    folder = world.publish("later", "1.0.0")
    folder.joinpath("pkg.toml").write_text('format = 3\n[source]\ndownload_url = "https://example.org/later.zip"\n', encoding="utf-8")

    required = world.cli("install", "future", "--json")
    unknown_format = world.cli("install", "later", "--json")
    check_updates = world.cli("install", "future", "--plan", "--json")

    assert required.code == 6 and required.document()["results"][0]["error"]["code"] == "needs-newer-gupkg"
    assert unknown_format.code == 6 and unknown_format.document()["results"][0]["error"]["code"] == "needs-newer-gupkg"
    assert check_updates.code == 6
    assert not (world.user / "future" / "current").exists()


def test_exit_code_4_is_an_internal_error_and_is_structured(world: CliWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """Something unexpected is exit code 4 with a structured error and no traceback."""
    def broken(*args, **kwargs):
        """Raise an unexpected error, as a bug would."""
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "run_batch", broken)

    result = world.cli("update", "tool", "--json")

    assert result.code == 4 and "Traceback" not in result.err + result.out
    assert result.document()["errors"][0]["code"] == "internal-error"


def test_an_internal_error_in_one_package_keeps_its_traceback_in_the_package_log_only(world: CliWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected exception of one target is exit code 4 for it; the traceback is in its log file, never on the console."""
    def broken(*args, **kwargs):
        """Raise an unexpected error, as a bug would."""
        raise RuntimeError("kaboom")

    monkeypatch.setattr(lifecycle, "_dispatch", broken)

    result = world.cli("update", "tool", "--json")

    assert result.code == 4 and "Traceback" not in result.err + result.out and "kaboom" in result.err
    [package_log] = (world.user / "tool" / ".gupkg" / "logs").glob("*-update.log")
    assert "DIAGNOSTIC: Traceback" in package_log.read_text(encoding="utf-8") and "RuntimeError: kaboom" in package_log.read_text(encoding="utf-8")


def test_exit_codes_2_3_and_5_are_distinguished(world: CliWorld) -> None:
    """A mistake of the caller is 2, a failed operation 3, a question that was not answered 5."""
    world.publish("broken", "1.0.0", source='require = ["missing.exe"]\n')

    assert world.cli("install", "unknown-package").code == 2
    assert world.cli("install", "broken").code == 3
    assert world.cli("uninstall", "tool", "--purge", "--no-input").code == 5
