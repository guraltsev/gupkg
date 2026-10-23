"""Cover which gupkg the generated 1.0 launchers (`install.cmd`, `gupkg-tui.cmd`) run.

Behavior covered: the launcher runs the root forwarder `<root>\\.gupkg\\gupkg.cmd` first and the
bundled `gupkg\\gupkg.cmd` otherwise; `--no-system-gupkg=true` skips the forwarder and is not
passed on; exit code 6 from the forwarder re-runs the command with the bundled gupkg; with
neither it exits 9009 and says how to get gupkg; the exit code of gupkg is the launcher's own;
`PATH` is never searched.

Real: the generated launchers run by the real ``cmd.exe`` in a temporary tree, with fake
`gupkg.cmd` files that print their arguments. Mocked: gupkg itself. Out of scope: the refusal
to run elevated, which needs an administrator shell (manual smoke test).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gupkg_migrate.launchers import LAUNCHERS, launcher_text, write_launchers

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the launchers are Windows cmd files")

FAKE = '@echo off\r\necho {label} %*\r\nexit /b %{variable}%\r\n'


def build_tree(root: Path, *, forwarder: bool, bundled: bool) -> Path:
    """Create `<root>/tool/1.0` with launchers, and fake forwarder and bundled gupkg as asked."""
    version = root / "tool" / "1.0"
    version.mkdir(parents=True)
    write_launchers(version)
    if forwarder:
        (root / ".gupkg").mkdir()
        (root / ".gupkg" / "gupkg.cmd").write_text(FAKE.format(label="FORWARDER", variable="FORWARDER_EXIT"), encoding="ascii")
    if bundled:
        (version / "gupkg").mkdir()
        (version / "gupkg" / "gupkg.cmd").write_text(FAKE.format(label="BUNDLED", variable="BUNDLED_EXIT"), encoding="ascii")
    return version


def run(launcher: Path, *args: str, forwarder_exit: int = 0, bundled_exit: int = 0, path: str = "") -> subprocess.CompletedProcess[str]:
    """Run a launcher with cmd.exe and return its output and exit code."""
    environment = {
        "SystemRoot": os.environ["SystemRoot"], "ComSpec": os.environ["ComSpec"], "GUPKG_NO_PAUSE": "1",
        "FORWARDER_EXIT": str(forwarder_exit), "BUNDLED_EXIT": str(bundled_exit), "PATH": path or os.environ["SystemRoot"] + "\\System32",
    }
    return subprocess.run(
        [os.environ["ComSpec"], "/d", "/c", str(launcher), *args],
        capture_output=True, text=True, env=environment, check=False, cwd=str(launcher.parent),
    )


def test_both_launchers_come_from_one_template_with_different_commands() -> None:
    """`install.cmd` runs `install` and `gupkg-tui.cmd` runs `tui`; everything else is identical."""
    install, tui = launcher_text("install"), launcher_text("tui")

    assert set(LAUNCHERS) == {"install.cmd", "gupkg-tui.cmd"}
    assert install.replace(" install ", " tui ").splitlines()[3:] == tui.splitlines()[3:]
    assert "\r\n" in install


def test_forwarder_runs_first_with_the_command_and_this_folder(tmp_path: Path) -> None:
    """The installed gupkg of the root is used when it exists, called with `install` and the version folder."""
    version = build_tree(tmp_path, forwarder=True, bundled=True)

    result = run(version / "install.cmd", "--refresh")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("FORWARDER install ")
    assert f'"{version}\\."' in result.stdout and "--refresh" in result.stdout
    assert "BUNDLED" not in result.stdout


def test_tui_launcher_runs_the_tui_command(tmp_path: Path) -> None:
    """`gupkg-tui.cmd` asks gupkg for its text interface on this folder."""
    version = build_tree(tmp_path, forwarder=True, bundled=False)

    assert run(version / "gupkg-tui.cmd").stdout.startswith("FORWARDER tui ")


def test_bundled_gupkg_is_used_when_the_root_has_no_forwarder(tmp_path: Path) -> None:
    """Without a root forwarder the gupkg bundled in the version folder runs."""
    version = build_tree(tmp_path, forwarder=False, bundled=True)

    result = run(version / "install.cmd")

    assert result.stdout.startswith("BUNDLED install ")


def test_no_system_gupkg_option_skips_the_forwarder_and_is_not_passed_on(tmp_path: Path) -> None:
    """`--no-system-gupkg=true` forces the bundled gupkg and is consumed by the launcher."""
    version = build_tree(tmp_path, forwarder=True, bundled=True)

    result = run(version / "install.cmd", "--no-system-gupkg=true", "--yes")

    assert result.stdout.startswith("BUNDLED install ")
    assert result.stdout.rstrip().endswith('1.0\\." "--yes"')


def test_no_system_gupkg_false_keeps_the_forwarder(tmp_path: Path) -> None:
    """`--no-system-gupkg=false` is consumed too and leaves the forwarder in use."""
    version = build_tree(tmp_path, forwarder=True, bundled=True)

    result = run(version / "install.cmd", "--no-system-gupkg=false", "--yes")

    assert result.stdout.startswith("FORWARDER install ")
    assert result.stdout.rstrip().endswith('1.0\\." "--yes"')


def test_exit_code_6_from_the_forwarder_falls_back_to_the_bundled_gupkg(tmp_path: Path) -> None:
    """An installed gupkg that cannot handle the package exits 6; the launcher says so and re-runs with the bundled one."""
    version = build_tree(tmp_path, forwarder=True, bundled=True)

    result = run(version / "install.cmd", forwarder_exit=6, bundled_exit=0)

    assert result.returncode == 0
    assert "FORWARDER install" in result.stdout and "BUNDLED install" in result.stdout
    assert "cannot handle this package" in result.stderr


def test_exit_code_6_without_a_bundled_gupkg_is_returned_unchanged(tmp_path: Path) -> None:
    """With no bundled gupkg to fall back to, exit code 6 is the launcher's exit code."""
    version = build_tree(tmp_path, forwarder=True, bundled=False)

    assert run(version / "install.cmd", forwarder_exit=6).returncode == 6


def test_the_exit_code_of_gupkg_is_the_launchers_exit_code(tmp_path: Path) -> None:
    """A failing gupkg makes the launcher fail with the same code."""
    version = build_tree(tmp_path, forwarder=False, bundled=True)

    assert run(version / "install.cmd", bundled_exit=3).returncode == 3


def test_without_any_gupkg_the_launcher_exits_9009_and_says_how_to_get_one(tmp_path: Path) -> None:
    """With neither a forwarder nor a bundled gupkg the launcher exits 9009 and names the bootstrap."""
    version = build_tree(tmp_path, forwarder=False, bundled=False)

    result = run(version / "install.cmd")

    assert result.returncode == 9009
    assert "gupkg-bootstrap.cmd" in result.stderr


def test_a_gupkg_on_path_is_never_used(tmp_path: Path) -> None:
    """A `gupkg.cmd` and `gupkg.exe` earlier on PATH are ignored: the launcher does not search PATH."""
    version = build_tree(tmp_path, forwarder=False, bundled=False)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "gupkg.cmd").write_text(FAKE.format(label="PATHGUPKG", variable="BUNDLED_EXIT"), encoding="ascii")
    (decoy / "gupkg.exe").write_text("not a program", encoding="ascii")

    result = run(version / "install.cmd", path=f"{decoy};{os.environ['SystemRoot']}\\System32")

    assert result.returncode == 9009
    assert "PATHGUPKG" not in result.stdout
