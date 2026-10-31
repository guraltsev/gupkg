"""Cover the commands of a gupkg tree: ``gupkg.cmd``, ``gupkg-tui.cmd`` and ``gupkg-manager.cmd`` at the root of the tree.

Behavior covered: each command starts only the launcher of the same name inside the ``gupkg`` folder beside it, forwards its
arguments unchanged (spaces included) and returns its exit code; it never uses a ``gupkg`` found on ``PATH`` or in the current
directory (design 21.3); with the inner launcher missing it says so and exits 9009.

Real: the Windows command interpreter and the checked-in wrappers; the inner launchers are small recording batch files.
Out of scope: what the inner launchers start (test_launcher, test_runtime_cli) and the package launchers (test_package_launcher).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows batch wrapper behavior")


def tree_with(root: Path, wrapper: str, inner_exit: int = 0) -> tuple[Path, Path]:
    """Copy *wrapper* into *root* with a recording inner launcher of the same name in ``gupkg\\``; return both paths and the log."""
    shutil.copy2(SRC_ROOT / wrapper, root / wrapper)
    (root / "gupkg").mkdir()
    log = root / "args.log"
    (root / "gupkg" / wrapper).write_text(f'@echo off\r\n> "{log}" echo args=%*\r\nexit /b {inner_exit}\r\n', encoding="ascii")
    return root / wrapper, log


def run(wrapper: Path, *arguments: str, cwd: Path, path: str = "") -> subprocess.CompletedProcess[str]:
    """Run a wrapper through ``cmd`` with a controlled PATH."""
    environment = {**os.environ, "PATH": path}
    return subprocess.run(["cmd", "/c", str(wrapper), *arguments], cwd=str(cwd), env=environment, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("name", ["gupkg.cmd", "gupkg-tui.cmd", "gupkg-manager.cmd"])
def test_each_command_starts_its_inner_launcher_with_the_same_arguments_and_exit_code(tmp_path: Path, name: str) -> None:
    """The inner launcher of the same name runs, receives every argument, and its exit code is the command's."""
    wrapper, log = tree_with(tmp_path, name, inner_exit=17)

    result = run(wrapper, "--name", "value with spaces", cwd=tmp_path)

    assert result.returncode == 17, result.stderr
    assert log.read_text(encoding="utf-8").strip() == 'args=--name "value with spaces"'


@pytest.mark.parametrize("name", ["gupkg.cmd", "gupkg-tui.cmd", "gupkg-manager.cmd"])
def test_a_missing_inner_launcher_exits_9009_and_nothing_on_path_or_in_the_current_directory_runs(tmp_path: Path, name: str) -> None:
    """With no inner launcher the command fails with 9009; a gupkg on PATH and one in the current directory are not used."""
    shutil.copy2(SRC_ROOT / name, tmp_path / name)
    decoys = tmp_path / "decoys"
    decoys.mkdir()
    marker = tmp_path / "decoy-ran.txt"
    for folder in (decoys, tmp_path / "cwd"):
        folder.mkdir(exist_ok=True)
        for decoy in ("gupkg.cmd", "gupkg-tui.cmd", "gupkg-manager.cmd"):
            (folder / decoy).write_text(f'@echo off\r\necho ran> "{marker}"\r\n', encoding="ascii")

    result = run(tmp_path / name, cwd=tmp_path / "cwd", path=str(decoys))

    assert result.returncode == 9009
    assert "incomplete" in result.stderr and not marker.exists()
    # The message says what the file is and what a person who copied it into a package folder needs instead.
    assert "gupkg folder" in result.stderr and "manage.cmd" in result.stderr and "pkg launchers" in result.stderr
