"""Cover how the command line builds the one scope of a run: the mode is the token's, nothing selects it, and the run says so.

Behavior covered: a normal process installs into the user root and an elevated one into the system root, neither run taking a
flag; ``--mode`` in any position and ``system:`` / ``user:`` target prefixes are usage errors (exit code 2) and change nothing; an
elevated run whose gupkg tree standard users can change fails at start with exit code 2, before any root is read or written;
``--root`` names the root and is reported as the source; the Resolve phase, the run log and the package log name the mode, the
root and the source (``<mode> root <path> (<source>)``); a root-wide ``cleanup`` prunes the run's root only; the elevated child of a
relaunch (still present until issue 035 T07) takes no root option, refuses a folder outside the system root and writes its
events file into the system root; a gupkg installed in another root than the run's is refused by ``self update`` with a clear
message.

Real: the argument parser, ``build_scope``, the engine, the run session and log files, real folders. Mocked: ``FakeSystem``
(elevation flag and the admin-only judgement) and the environment mapping built by the test.
Out of scope: the relaunch itself (``tests/test_cli_relaunch.py``) and building a scope from files (``tests/test_scope.py``).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from gupkg.engine.locks import Holder, lock_path, render_lock
from tests.cli_support import CliWorld
from tests.engine_support import zip_bytes
from tests.mode_support import pin_no_ambient_elevation, tree_snapshot  # noqa: F401


def test_a_normal_process_installs_into_the_user_root_and_an_elevated_one_into_the_system_root(tmp_path: Path) -> None:
    """The same command, with no flag: the token alone decides the mode and so the root."""
    normal, elevated = CliWorld(tmp_path / "a"), CliWorld(tmp_path / "b", elevated=True)
    normal.publish("tool", "1.0.0")
    elevated.publish("tool", "1.0.0", mode="system")

    user_run = normal.cli("install", "tool", "--json")
    system_run = elevated.cli("install", "tool", "--json")

    assert user_run.code == 0 and system_run.code == 0, (user_run.err, system_run.err)
    assert (normal.user / "tool" / "current").is_dir() and not (normal.system_root / "tool" / "current").exists()
    assert (elevated.system_root / "tool" / "current").is_dir() and not (elevated.user / "tool" / "current").exists()
    assert user_run.document()["results"][0]["mode"] == "user" and system_run.document()["results"][0]["mode"] == "system"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("install", "tool", "--mode", "user"), id="mode-user"),
        pytest.param(("install", "tool", "--mode", "system"), id="mode-system"),
        pytest.param(("--mode", "user", "install", "tool"), id="mode-before-the-command"),
        pytest.param(("install", "system:tool"), id="system-prefix"),
        pytest.param(("install", "user:tool"), id="user-prefix"),
        pytest.param(("info", "USER:tool"), id="prefix-in-another-case"),
        pytest.param(("list", "--mode", "user"), id="mode-of-list"),
    ],
)
def test_the_mode_option_and_the_selector_prefixes_are_usage_errors(tmp_path: Path, argv: tuple[str, ...]) -> None:
    """Nothing selects a mode any more: each old spelling is exit code 2, before anything is read or written."""
    world = CliWorld(tmp_path)
    world.publish("tool", "1.0.0")
    before = tree_snapshot(world.user)

    result = world.cli(*argv)

    assert result.code == 2, result.out + result.err
    assert not (world.user / "tool" / "current").exists()
    assert tree_snapshot(world.user) == before


def test_an_elevated_run_in_a_user_writable_gupkg_fails_at_start_with_exit_2_and_touches_nothing(tmp_path: Path) -> None:
    """An administrator must not run code that standard users can change: the run ends at start, whatever the command is."""
    world = CliWorld(tmp_path, elevated=True, untrusted={tmp_path / "gupkg-tree"})
    world.publish("tool", "1.0.0", mode="system")
    before = tree_snapshot(tmp_path)

    results = [world.cli("install", "tool", "--yes"), world.cli("list"), world.cli("settings", "show"), world.cli("check")]

    assert [item.code for item in results] == [2, 2, 2, 2], [item.err for item in results]
    assert all("admin-only root" in item.err for item in results)
    assert tree_snapshot(tmp_path) == before


def test_commands_that_need_no_root_build_no_scope(tmp_path: Path) -> None:
    """``pkg new`` works with a broken ``--settings`` file and from an elevated run in a user-writable gupkg (main does too)."""
    bad = tmp_path / "bad.toml"
    bad.write_text("schema = [", encoding="utf-8")
    world = CliWorld(tmp_path / "a")
    elevated = CliWorld(tmp_path / "b", elevated=True, untrusted={tmp_path / "b" / "gupkg-tree"})

    broken = world.cli("--settings", str(bad), "pkg", "new", "mypk", "--dir", str(tmp_path / "out1"))
    unsafe = elevated.cli("pkg", "new", "mypk", "--dir", str(tmp_path / "out2"))

    assert broken.code == 0, broken.err
    assert unsafe.code == 0, unsafe.err
    assert world.cli("--settings", str(bad), "list").code == 2


def test_root_names_the_root_and_is_the_reported_source(tmp_path: Path) -> None:
    """``--root DIR`` is a root option: the run works there, and the Resolve line says the root came from ``--root``."""
    world = CliWorld(tmp_path)
    other = tmp_path / "elsewhere"
    world.publish("tool", "1.0.0", root=other)

    result = world.cli("--root", str(other), "install", "tool")

    assert result.code == 0, result.err
    assert (other / "tool" / "current").is_dir() and not (world.user / "tool").exists()
    assert f"user root {other} (--root)" in result.out


def test_the_resolve_line_and_the_logs_name_the_mode_the_root_and_the_source(tmp_path: Path) -> None:
    """``<mode> root <path> (<source>)`` is printed in the Resolve phase and written into the run log and the package log."""
    world = CliWorld(tmp_path)
    world.publish("tool", "1.0.0")
    expected = f"user root {world.user} ($App\\settings.toml)"

    result = world.cli("install", "tool")

    assert result.code == 0, result.err
    assert expected in result.out
    [run_log] = list((world.user / ".gupkg" / "logs").glob("*-install-tool.log"))
    [package_log] = list((world.user / "tool" / ".gupkg" / "logs").glob("*-install.log"))
    assert f"# scope: {expected}" in run_log.read_text(encoding="utf-8")
    package_text = package_log.read_text(encoding="utf-8")
    assert f"# scope: {expected}" in package_text and f"  {expected}" in package_text


def test_the_plan_of_an_elevated_run_names_the_system_root_and_its_source(tmp_path: Path) -> None:
    """``--plan --json`` carries the same text in the Resolve phase."""
    world = CliWorld(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system")

    result = world.cli("install", "tool", "--plan", "--json")

    resolve = result.document()["plans"][0]["plan"]["phases"][0]
    assert resolve["name"] == "Resolve"
    assert f"system root {world.system_root} (the installed system gupkg)" in resolve["steps"]


def test_a_root_wide_cleanup_prunes_only_the_root_of_the_run(tmp_path: Path) -> None:
    """``cleanup`` with no target covers the packages and the logs of the run's root, and the document says which mode that was."""
    world = CliWorld(tmp_path)
    world.publish("mine", "1.0.0")
    world.publish("theirs", "1.0.0", mode="system")

    result = world.cli("cleanup", "--yes", "--json")

    assert result.code == 0, result.err
    document = result.document()
    assert set(document["root_cleanup"]) == {"user"} and [item["package"] for item in document["results"]] == ["mine"]


def test_self_update_in_a_run_whose_root_is_not_the_gupkgs_own_is_refused_with_a_clear_message(tmp_path: Path) -> None:
    """A system gupkg run from a normal shell would look for ``gupkg`` in the user root: say so instead of 'not installed'."""
    world = CliWorld(tmp_path)

    result = world.cli("self", "update")

    assert result.code == 2 and "This gupkg is installed in" in result.err and "administrator shell" in result.err


# ---------------------------------------------------------------------------
# The elevated run and the elevated child of a relaunch (the child goes with issue 035 T07)
# ---------------------------------------------------------------------------


def test_an_elevated_install_writes_the_events_file_into_the_system_root(tmp_path: Path) -> None:
    """The events file named by ``--events-file`` is written into the system root's log folders and ends with the result document."""
    world = CliWorld(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system")

    result = world.cli("install", "tool", "--events-file", "run.jsonl", "--yes")

    assert result.code == 0, result.err
    assert (world.system_root / "tool" / "current").is_dir()
    run_events = world.system_root / ".gupkg" / "logs" / "run.jsonl"
    package_events = world.system_root / "tool" / ".gupkg" / "logs" / "run.jsonl"
    assert run_events.is_file() and package_events.is_file()
    records = [json.loads(line) for line in run_events.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "run_end" and records[-1]["exit_code"] == 0
    assert records[-1]["result"]["api"] == 1 and records[-1]["result"]["results"][0]["package"] == "tool"
    assert any(record["event"] == "log_link" for record in records)


def test_the_elevated_child_refuses_a_folder_outside_the_system_root(tmp_path: Path) -> None:
    """A distribution folder is data from a user-writable place: the relaunched child refuses it, whatever the parent said."""
    world = CliWorld(tmp_path, elevated=True)
    folder = world.machine.distribution("tool2", "1.0.0")

    result = world.cli("install", str(folder), "--relaunched", "--yes", "--json")

    assert result.code == 2
    assert "accepts only package names and paths inside the system root" in result.document()["results"][0]["error"]["message"]
    assert not (world.system_root / "tool2").exists()


def test_a_direct_administrator_shell_may_copy_a_distribution_in_but_the_relaunched_child_may_not(tmp_path: Path) -> None:
    """The same folder is accepted by an administrator's own shell (it is copied into the system root) and refused as a relaunch."""
    world = CliWorld(tmp_path, elevated=True)
    folder = world.machine.distribution("tool2", "1.0.0")
    world.fake.add_url("https://example.org/tool-1.0.0.zip", zip_bytes({"app.exe": "tool2 1.0.0"}))

    refused = world.cli("install", str(folder), "--relaunched", "--yes", "--skip-checksum")
    accepted = world.cli("install", str(folder), "--yes", "--skip-checksum")

    assert refused.code == 2 and accepted.code == 0, accepted.err
    assert (world.system_root / "tool2" / "1.0.0" / "pkg.toml").is_file()


def test_the_elevated_child_refuses_the_root_options(tmp_path: Path) -> None:
    """``--settings``, ``--root`` and ``--sandbox`` could steer an elevated child: they are refused before anything is done."""
    world = CliWorld(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system")
    settings = tmp_path / "x.toml"
    settings.write_text("schema = 4\n", encoding="utf-8")

    steered = world.cli("--settings", str(settings), "install", "tool", "--relaunched", "--yes")
    rooted = world.cli("--root", str(tmp_path / "other"), "install", "tool", "--relaunched", "--yes")
    sandboxed = world.cli("--sandbox", str(tmp_path / "sb"), "install", "tool", "--relaunched", "--yes")

    for refused in (steered, rooted, sandboxed):
        assert refused.code == 2 and "not accepted" in refused.err
    assert not (world.system_root / "tool" / "current").exists()


def test_the_elevated_sweep_leaves_a_busy_system_package_alone(tmp_path: Path) -> None:
    """A root-wide cleanup skips a package another run holds (it does not fail package-busy) and prunes the root."""
    world = CliWorld(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system")
    world.publish("other", "1.0.0", mode="system")
    assert world.cli("install", "tool", "--yes").code == 0
    lock = lock_path(world.system_root / "tool")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(render_lock(Holder(424242, socket.gethostname(), "update", "2026-10-24T10:00:00", "othertoken")), encoding="utf-8")
    world.fake.live_pids.add(424242)

    result = world.cli("cleanup", "--yes", "--json")

    document = result.document()
    assert result.code == 0, result.err
    assert {item["package"]: item["status"] for item in document["results"]} == {"other": "cleaned", "tool": "locked"}
    assert "system" in document["root_cleanup"]
