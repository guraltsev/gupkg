"""Cover how the command line builds the one scope of a run: the mode is the token's, nothing selects it, and the run says so.

Behavior covered: a normal process installs into the user root and an elevated one into the system root, neither run taking a
flag; ``--mode`` in any position and ``system:`` / ``user:`` target prefixes are usage errors (exit code 2) and change nothing; an
elevated run whose gupkg tree standard users can change fails at launch with exit code 2, before any root is opened or written;
``--root`` names the root and is reported as the source; the Resolve phase, the run log and the package log name the mode, the
root and the source (``<mode> root <path> (<source>)``); a root-wide ``cleanup`` prunes the run's root only; an elevated run writes its
events file into the system root and may copy a distribution folder in; a gupkg installed in another root than the run's is refused by ``self update`` with a clear
message.

Real: the argument parser, ``build_scope``, the engine, the run session and log files, real folders. Mocked: ``FakeSystem``
(elevation flag and the admin-only judgement) and the environment mapping built by the test.
Out of scope: building a scope from files (``tests/test_scope.py``).
"""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from gupkg.engine.locks import Holder, lock_path, render_lock
from gupkg.model.scope import SYSTEM_ONLY_REFUSAL
from tests.support.cli import CliWorld
from tests.support.engine import zip_bytes
from tests.support.mode import tree_snapshot

pytestmark = pytest.mark.unit


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
def test_the_mode_option_and_the_selector_prefixes_are_usage_errors(cli_world: CliWorld, argv: tuple[str, ...]) -> None:
    """Nothing selects a mode any more: each old spelling is exit code 2, before anything is read or written."""
    cli_world.publish("tool", "1.0.0")
    before = tree_snapshot(cli_world.user)

    result = cli_world.cli(*argv)

    assert result.code == 2, result.out + result.err
    assert not (cli_world.user / "tool" / "current").exists()
    assert tree_snapshot(cli_world.user) == before


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


def test_root_names_the_root_and_is_the_reported_source(tmp_path: Path, cli_world: CliWorld) -> None:
    """``--root DIR`` is a root option: the run works there, and the Resolve line says the root came from ``--root``."""
    other = tmp_path / "elsewhere"
    cli_world.publish("tool", "1.0.0", root=other)

    result = cli_world.cli("--root", str(other), "install", "tool")

    assert result.code == 0, result.err
    assert (other / "tool" / "current").is_dir() and not (cli_world.user / "tool").exists()
    assert f"user root {other} (--root)" in result.out


def test_the_resolve_line_and_the_logs_name_the_mode_the_root_and_the_source(cli_world: CliWorld) -> None:
    """``<mode> root <path> (<source>)`` is printed in the Resolve phase and written into the run log and the package log."""
    cli_world.publish("tool", "1.0.0")
    expected = f"user root {cli_world.user} ($App\\settings.toml)"

    result = cli_world.cli("install", "tool")

    assert result.code == 0, result.err
    assert expected in result.out
    [run_log] = list((cli_world.user / ".gupkg" / "logs").glob("*-install-tool.log"))
    [package_log] = list((cli_world.user / "tool" / ".gupkg" / "logs").glob("*-install.log"))
    assert f"# scope: {expected}" in run_log.read_text(encoding="utf-8")
    package_text = package_log.read_text(encoding="utf-8")
    assert f"# scope: {expected}" in package_text and f"  {expected}" in package_text


def test_the_plan_of_an_elevated_run_names_the_system_root_and_its_source(cli_elevated_world: CliWorld) -> None:
    """``--plan --json`` carries the same text in the Resolve phase."""
    cli_elevated_world.publish("tool", "1.0.0", mode="system")

    result = cli_elevated_world.cli("install", "tool", "--plan", "--json")

    resolve = result.document()["plans"][0]["plan"]["phases"][0]
    assert resolve["name"] == "Resolve"
    assert f"system root {cli_elevated_world.system_root} (the installed system gupkg)" in resolve["steps"]


def test_a_root_wide_cleanup_prunes_only_the_root_of_the_run(cli_world: CliWorld) -> None:
    """``cleanup`` with no target covers the packages and the logs of the run's root, and the document says which mode that was."""
    cli_world.publish("mine", "1.0.0")
    cli_world.publish("theirs", "1.0.0", mode="system")

    result = cli_world.cli("cleanup", "--yes", "--json")

    assert result.code == 0, result.err
    document = result.document()
    assert set(document["root_cleanup"]) == {"user"} and [item["package"] for item in document["results"]] == ["mine"]


def test_self_update_in_a_run_whose_root_is_not_the_gupkgs_own_is_refused_with_a_clear_message(cli_world: CliWorld) -> None:
    """A system gupkg run from a normal shell would look for ``gupkg`` in the user root: say so instead of 'not installed'."""

    result = cli_world.cli("self", "update")

    assert result.code == 2 and "This gupkg is installed in" in result.err and "administrator shell" in result.err


# ---------------------------------------------------------------------------
# The elevated run
# ---------------------------------------------------------------------------


def test_an_elevated_install_writes_the_events_file_into_the_system_root(cli_elevated_world: CliWorld) -> None:
    """The events file named by ``--events-file`` is written into the system root's log folders and ends with the result document."""
    # Arrange: a published system package on an elevated machine.
    cli_elevated_world.publish("tool", "1.0.0", mode="system")

    # Act: install it with an events file.
    result = cli_elevated_world.cli("install", "tool", "--events-file", "run.jsonl", "--yes")

    # Assert: the package installed and both events files exist.
    assert result.code == 0, result.err
    assert (cli_elevated_world.system_root / "tool" / "current").is_dir()
    run_events = cli_elevated_world.system_root / ".gupkg" / "logs" / "run.jsonl"
    package_events = cli_elevated_world.system_root / "tool" / ".gupkg" / "logs" / "run.jsonl"
    assert run_events.is_file() and package_events.is_file()
    # The events file holds the run's records, ending with the result.
    records = [json.loads(line) for line in run_events.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "run_end" and records[-1]["exit_code"] == 0
    assert records[-1]["result"]["api"] == 2 and records[-1]["result"]["results"][0]["package"] == "tool"
    assert any(record["event"] == "log_link" for record in records)


def test_an_administrator_shell_may_copy_a_distribution_folder_into_the_system_root(cli_elevated_world: CliWorld) -> None:
    """A distribution folder outside the root is accepted by an administrator's own shell: it is copied into the system root."""
    folder = cli_elevated_world.machine.distribution("tool2", "1.0.0")
    cli_elevated_world.fake.add_url("https://example.org/tool-1.0.0.zip", zip_bytes({"app.exe": "tool2 1.0.0"}))

    accepted = cli_elevated_world.cli("install", str(folder), "--yes", "--skip-checksum")

    assert accepted.code == 0, accepted.err
    assert (cli_elevated_world.system_root / "tool2" / "1.0.0" / "pkg.toml").is_file()


def test_the_elevated_sweep_leaves_a_busy_system_package_alone(cli_elevated_world: CliWorld) -> None:
    """A root-wide cleanup skips a package another run holds (it does not fail package-busy) and prunes the root."""
    # Arrange: two system packages, one of them installed and locked by a live process.
    cli_elevated_world.publish("tool", "1.0.0", mode="system")
    cli_elevated_world.publish("other", "1.0.0", mode="system")
    assert cli_elevated_world.cli("install", "tool", "--yes").code == 0
    lock = lock_path(cli_elevated_world.system_root / "tool")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(render_lock(Holder(424242, socket.gethostname(), "update", "2026-10-24T10:00:00", "othertoken")), encoding="utf-8")
    cli_elevated_world.fake.live_pids.add(424242)

    # Act: sweep the root with ``cleanup``.
    result = cli_elevated_world.cli("cleanup", "--yes", "--json")

    # Assert: the busy package is left alone and the other one is cleaned.
    document = result.document()
    assert result.code == 0, result.err
    assert {item["package"]: item["status"] for item in document["results"]} == {"other": "cleaned", "tool": "locked"}
    assert "system" in document["root_cleanup"]


def test_a_normal_run_cannot_install_a_system_only_package_and_starts_no_process(cli_world: CliWorld) -> None:
    """No elevated copy of gupkg exists: the refusal names the Start Menu entry and the command, nothing runs, nothing is written."""
    cli_world.publish("tool", "1.0.0", top='modes = ["system"]')
    before = (tree_snapshot(cli_world.user), tree_snapshot(cli_world.system_root))

    result = cli_world.cli("install", "tool", "--json")

    assert result.code == 2, result.out + result.err
    assert result.document()["results"][0]["error"]["message"] == SYSTEM_ONLY_REFUSAL.replace("<command> <name>", "install tool")
    assert cli_world.fake.process_log == [] and cli_world.fake.foreground_log == []
    assert not (cli_world.user / "tool" / "current").exists() and not (cli_world.system_root / "tool" / "current").exists()
    assert (tree_snapshot(cli_world.user), tree_snapshot(cli_world.system_root)) == before


def test_purging_the_root_its_parent_or_a_drive_is_refused_before_anything_is_deleted(cli_world: CliWorld) -> None:
    """The executor's purge guard is gone because the catalog refuses these paths first: nothing is deleted."""
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool").code == 0
    before = tree_snapshot(cli_world.user)

    for target in (cli_world.user, cli_world.user.parent, Path(cli_world.user.anchor)):
        result = cli_world.cli("uninstall", str(target), "--purge", "--yes", "--json")

        assert result.code != 0, (target, result.out + result.err)
        assert cli_world.user.is_dir() and (cli_world.user / "tool" / "current").exists(), target
    assert tree_snapshot(cli_world.user) == before


@pytest.mark.process
def test_self_update_accepts_the_gupkgs_own_root_under_another_spelling(tmp_path: Path, cli_world: CliWorld) -> None:
    """A root named through a junction is the same root: the refusal compares folders, not spellings."""
    link = tmp_path / "link-to-system-root"
    cli_world.system_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(cli_world.system_root)], check=True, capture_output=True)

    result = cli_world.cli("--root", str(link), "self", "update", "--plan")

    assert "would look for it there" not in result.err and "administrator shell" not in result.err, result.err


def test_self_update_in_another_user_writable_root_is_refused_without_advising_an_administrator_shell(tmp_path: Path) -> None:
    """A gupkg that users can change, run on another root: the hint names ``--root``, not an administrator shell."""
    world = CliWorld(tmp_path, untrusted={tmp_path / "sys"})

    result = world.cli("self", "update")

    assert result.code == 2 and "This gupkg is installed in" in result.err and "would look for it there" in result.err
    assert "administrator shell" not in result.err and f"--root {world.system_root}" in result.err


def test_an_update_to_a_version_that_narrows_modes_names_the_version_and_keeps_the_installed_one(cli_world: CliWorld) -> None:
    """The refused new version is named, the installed one keeps working, and no administrator advice is given for an update."""
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool", "--yes").code == 0
    cli_world.publish("tool", "2.0.0", top='modes = ["system"]\n')

    result = cli_world.cli("update", "tool", "--yes", "--json")

    message = result.document()["results"][0]["error"]["message"]
    assert result.code == 2
    assert message == "tool 2.0.0 installs only system-wide, so it cannot be installed in this user root. The installed version 1.0.0 keeps working."
    assert (cli_world.user / "tool" / "current").resolve().name == "1.0.0"

@pytest.mark.parametrize(
    "prefix",
    [
        pytest.param(("--settings", "{bad}"), id="settings"),
        pytest.param(("--root", "{root}"), id="root"),
        pytest.param(("--settings", "{bad}", "--root", "{root}"), id="settings-and-root"),
        pytest.param(("--sandbox", "{sandbox}", "--settings", "{bad}"), id="sandbox"),
    ],
)
def test_a_setup_error_document_names_the_command_not_the_value_of_a_root_option(tmp_path: Path, prefix: tuple[str, ...]) -> None:
    """``command`` of the failure document is ``list`` whatever root options come first."""
    bad = tmp_path / "bad.toml"
    bad.write_text("schema = [\n", encoding="utf-8")
    (tmp_path / "r" / ".gupkg").mkdir(parents=True)
    (tmp_path / "r" / ".gupkg" / "settings.toml").write_text("schema = [\n", encoding="utf-8")
    values = {"bad": str(bad), "root": str(tmp_path / "r"), "sandbox": str(tmp_path / "sb")}
    world = CliWorld(tmp_path / "w")

    result = world.cli(*(item.format(**values) for item in prefix), "list", "--json")

    assert result.code == 2 and result.document()["command"] == "list", result.out

def test_a_root_option_after_the_command_still_reports_the_command(tmp_path: Path) -> None:
    """``list --root X --json`` is a usage error (the option is global), and its document names ``list``."""
    result = CliWorld(tmp_path).cli("list", "--root", str(tmp_path / "r"), "--json")

    assert result.code == 2 and result.document()["command"] == "list", result.out