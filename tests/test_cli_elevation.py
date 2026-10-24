"""Cover how the command line hands system-mode work to one elevated child, and how the child protects itself.

Behaviour covered: a normal shell that is asked for system targets starts one elevated child with a command line rebuilt from
the parsed options (the child's marker ``--relaunched`` always present, ``--settings``, ``--sandbox`` and ``--json`` never
forwarded, ``--yes`` only when the user gave it); a call that mixes system and user targets reports the user ones as not run;
the child, given that command line, installs in the admin-only system root, writes the events file named by ``--events-file``
into the system root's log directories (a non-elevated reader follows it to the result line), and refuses a folder outside
every root, a user root and the root options that a standard user could use to steer it; a gupkg that is not admin-only never
relaunches; a declined or unanswerable relaunch is exit code 5.

Real: the argument parser, the engine, the run session and log files, real folders. Mocked: ``FakeSystem`` (elevation flag, the
relaunch record, the admin-only judgement) and the environment mapping built by the test.
Out of scope: the real UAC prompt and ``ShellExecute`` (``tests/manual_smoke.md``).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from gupkg.engine.locks import Holder, lock_path, render_lock
from tests.cli_support import CliWorld
from tests.engine_support import zip_bytes
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A normal (non-elevated) fake machine whose stand-in gupkg is installed in the admin-only system root."""
    machine = CliWorld(tmp_path)
    machine.publish("tool", "1.0.0", mode="system")
    machine.publish("other", "1.0.0", mode="system")
    return machine


def child_of(world: CliWorld, *argv: str, stdin: str = "y\n", code: int = 0) -> list[str]:
    """Run *argv* in the normal shell, assert that it relaunched once, and return the child's command line.

    A fake child writes no events file, so a call that names ``--events-file`` ends with *code* 3 (no ``run_end`` to read).
    """
    result = world.cli(*argv, stdin=stdin)
    assert result.code == code, result.err
    assert len(world.fake.relaunches) == 1, world.fake.relaunches
    return world.fake.relaunches[0]


def test_system_targets_start_one_elevated_child_with_a_rebuilt_command_line(world: CliWorld) -> None:
    """The child gets the command, the targets, the whitelisted options and --relaunched; nothing is installed by the parent."""
    argv = child_of(world, "install", "system:tool", "system:other", "--refresh", "--events-file", "run.jsonl", "--yes", code=3)

    assert argv[0] == "install" and argv[-3:] == ["--", "system:tool", "system:other"]
    assert "--relaunched" in argv and "--refresh" in argv and "--yes" in argv
    assert argv[argv.index("--events-file") + 1] == "run.jsonl"
    assert not (world.system_root / "tool" / "current").exists()


def test_the_child_command_line_never_carries_settings_sandbox_or_json(world: CliWorld, tmp_path: Path) -> None:
    """--settings and --sandbox are root options a standard user could use to steer the child; --json is the parent's alone."""
    settings = tmp_path / "evil.toml"
    settings.write_text("schema = 3\n", encoding="utf-8")

    result = world.cli("--settings", str(settings), "install", "system:tool", "--json", "--yes")

    assert result.code == 0, result.err
    argv = world.fake.relaunches[0]
    assert not {"--settings", "--sandbox", "--json", str(settings)} & set(argv)


def test_yes_is_forwarded_only_when_the_user_gave_it(world: CliWorld) -> None:
    """An answer the parent obtained ("run this on 2 packages?") is passed on as --batch-confirmed, never as --yes."""
    argv = child_of(world, "install", "system:tool", "system:other", stdin="y\ny\n")

    assert "--yes" not in argv and "--batch-confirmed" in argv


def test_the_relaunch_question_is_asked_once_and_no_input_refuses_it(world: CliWorld) -> None:
    """Without --yes the relaunch is a question: --no-input is exit code 5, a "no" is exit code 5, and nothing starts."""
    refused = world.cli("install", "system:tool", "--no-input", "--json")
    declined = world.cli("install", "system:tool", stdin="n\n")

    assert refused.code == 5 and refused.document()["results"][0]["error"]["code"] == "input-needed"
    assert declined.code == 5
    assert world.fake.relaunches == []


def test_no_input_refuses_the_relaunch_even_when_standard_input_holds_an_answer(world: CliWorld) -> None:
    """The elevation question is never asked under --no-input (exit code 5, no relaunch), and --yes never reads standard input."""
    refused = world.cli("install", "system:tool", "--no-input", "--json", stdin="y\n")
    assert refused.code == 5 and refused.document()["results"][0]["error"]["code"] == "input-needed"
    assert world.fake.relaunches == []

    accepted = world.cli("install", "system:tool", "--yes", stdin=None)
    assert accepted.code == 0 and len(world.fake.relaunches) == 1


def test_qualified_targets_of_both_modes_run_the_user_ones_here_and_hand_the_system_ones_to_the_child(world: CliWorld) -> None:
    """Targets that name their mode are split: the user target is installed by this process, the system one is relaunched."""
    world.publish("mine", "1.0.0", mode="user")

    result = world.cli("install", "system:tool", "user:mine", "--yes", "--json")

    document = result.document()
    assert result.code == 0 and document["relaunched"] is True and len(world.fake.relaunches) == 1
    assert world.fake.relaunches[0][0] == "install" and world.fake.relaunches[0][-2:] == ["--", "system:tool"] and "user:mine" not in world.fake.relaunches[0]
    assert (world.user / "mine" / "current").is_dir() and not (world.system_root / "tool" / "current").exists()
    assert [item["package"] for item in document["results"]] == ["mine"]


def test_unqualified_targets_of_both_modes_report_the_user_one_as_not_run(world: CliWorld) -> None:
    """When the modes are known only after resolving, the elevated child cannot take the user target: it is reported, not dropped."""
    world.publish("mine", "1.0.0", mode="user")

    result = world.cli("install", "tool", "mine", "--yes", "--json")

    document = result.document()
    assert result.code == 2 and document["relaunched"] is True and len(world.fake.relaunches) == 1
    entry = document["results"][0]
    assert (entry["package"], entry["status"], entry["error"]["code"]) == ("mine", "not-run", "mixed-modes")
    assert not (world.user / "mine" / "current").exists()


def test_a_gupkg_that_is_not_admin_only_never_relaunches(tmp_path: Path) -> None:
    """A user-writable gupkg refuses a system install: exit code 2, nothing is started, and the message says why."""
    world = CliWorld(tmp_path)
    world.publish("tool", "1.0.0", mode="system")
    world.fake.untrusted_dirs.add(world.location.version_dir.parent)

    result = world.cli("install", "system:tool", "--yes")

    assert result.code == 2 and "System installs need the system gupkg" in result.out + result.err
    assert world.fake.relaunches == []


def test_a_source_checkout_that_cannot_name_itself_never_relaunches(tmp_path: Path) -> None:
    """A gupkg that does not know where its own folders are cannot prove them admin-only, so it does not elevate."""
    world = CliWorld(tmp_path, installed=False)
    world.publish("tool", "1.0.0", mode="system")

    result = world.cli("install", "system:tool", "--yes")

    assert result.code == 2 and world.fake.relaunches == []


def test_the_child_installs_in_the_system_root_and_writes_the_events_file_there(world: CliWorld) -> None:
    """Run the recorded child command line elevated: the install happens, and the events file ends with the result document."""
    argv = child_of(world, "install", "system:tool", "--events-file", "run.jsonl", "--yes", code=3)
    world.fake.elevated = True

    child = world.cli(*argv)

    assert child.code == 0, child.err
    assert (world.system_root / "tool" / "current").is_dir()
    run_events = world.system_root / ".gupkg" / "logs" / "run.jsonl"
    package_events = world.system_root / "tool" / ".gupkg" / "logs" / "run.jsonl"
    assert run_events.is_file() and package_events.is_file()
    records = [json.loads(line) for line in run_events.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "run_end" and records[-1]["exit_code"] == 0
    assert records[-1]["result"]["api"] == 1 and records[-1]["result"]["results"][0]["package"] == "tool"
    assert any(record["event"] == "log_link" for record in records)


def test_the_child_refuses_a_folder_outside_every_root(world: CliWorld, tmp_path: Path) -> None:
    """A distribution folder is data from a user-writable place: the relaunched child refuses it, whatever the parent said."""
    folder = world.machine.distribution("tool2", "1.0.0")
    world.fake.elevated = True

    result = world.cli("install", str(folder), "--mode", "system", "--relaunched", "--yes", "--json")

    assert result.code == 2
    assert "accepts only package names and paths inside the system root" in result.document()["results"][0]["error"]["message"]
    assert not (world.system_root / "tool2").exists()


def test_a_direct_administrator_shell_may_copy_a_distribution_in_but_the_relaunched_child_may_not(world: CliWorld) -> None:
    """The same folder is accepted by an administrator's own shell (it is copied into the system root) and refused as a relaunch."""
    folder = world.machine.distribution("tool2", "1.0.0")
    world.fake.add_url("https://example.org/tool-1.0.0.zip", zip_bytes({"app.exe": "tool2 1.0.0"}))
    world.fake.elevated = True

    refused = world.cli("install", str(folder), "--mode", "system", "--relaunched", "--yes", "--skip-checksum")
    accepted = world.cli("install", str(folder), "--mode", "system", "--yes", "--skip-checksum")

    assert refused.code == 2 and accepted.code == 0, accepted.err
    assert (world.system_root / "tool2" / "1.0.0" / "pkg.toml").is_file()


def test_the_child_refuses_a_user_root_and_the_root_options(world: CliWorld, tmp_path: Path) -> None:
    """An elevated child never acts on a user root, and rejects --settings and --sandbox before doing anything."""
    world.publish("mine", "1.0.0", mode="user")
    world.fake.elevated = True
    settings = tmp_path / "x.toml"
    settings.write_text("schema = 3\n", encoding="utf-8")

    user = world.cli("install", "user:mine", "--relaunched", "--yes", "--json")
    steered = world.cli("--settings", str(settings), "install", "system:tool", "--relaunched", "--yes")
    sandboxed = world.cli("--sandbox", str(tmp_path / "sb"), "install", "system:tool", "--relaunched", "--yes")

    assert user.code == 2 and "elevated" in user.document()["results"][0]["error"]["message"]
    assert steered.code == 2 and "not accepted" in steered.err
    assert sandboxed.code == 2 and "not accepted" in sandboxed.err
    assert not (world.system_root / "tool" / "current").exists()


def test_a_root_wide_cleanup_keeps_its_meaning_across_the_relaunch(world: CliWorld) -> None:
    """`cleanup` with no target from a normal shell hands the child no targets, so the child sweeps and prunes the system root itself."""
    world.publish("mine", "1.0.0", mode="user")

    result = world.cli("cleanup", "--yes", "--json")

    assert result.code == 0, result.err
    [argv] = world.fake.relaunches
    assert argv[0] == "cleanup" and not [item for item in argv if item.startswith(("system:", "user:"))]
    document = result.document()
    assert document["relaunched"] is True and set(document["root_cleanup"]) == {"user"}


def test_the_relaunched_sweep_leaves_a_busy_system_package_alone(world: CliWorld) -> None:
    """The child of a root-wide cleanup skips a package another run holds (it does not fail package-busy) and prunes the root."""
    world.fake.elevated = True
    assert world.cli("install", "system:tool", "--yes").code == 0
    lock = lock_path(world.system_root / "tool")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(render_lock(Holder(424242, socket.gethostname(), "update", "2026-10-24T10:00:00", "othertoken")), encoding="utf-8")
    world.fake.live_pids.add(424242)

    child = world.cli("cleanup", "--relaunched", "--yes", "--json")

    document = child.document()
    assert child.code == 0, child.err
    assert {item["package"]: item["status"] for item in document["results"]} == {"other": "cleaned", "tool": "locked"}
    assert "system" in document["root_cleanup"]


def test_check_updates_never_relaunches_and_an_elevated_one_on_a_user_package_refuses(world: CliWorld) -> None:
    """check-updates of a system package from a normal shell only reports; elevated, a user package is refused (D31)."""
    world.tracker("tool", mode="system")
    world.announce("tool", "1.1.0")

    normal = world.cli("check-updates", "system:tool", "--json")
    world.publish("mine", "1.0.0", mode="user")
    world.tracker("mine", mode="user")
    world.fake.elevated = True
    elevated = world.cli("check-updates", "user:mine", "--json")

    assert normal.code == 0 and world.fake.relaunches == []
    assert normal.document()["results"][0]["details"]["written"] is False
    assert elevated.code == 2
