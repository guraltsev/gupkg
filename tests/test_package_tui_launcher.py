"""Cover the per-package gupkg-tui.cmd that registry packages ship.

Behavior covered: it opens gupkg's text interface for its own folder using a gupkg
installed beside it, falls back to gupkg.exe on PATH, ignores the current directory,
passes the interface's exit status through, and explains how to install gupkg when
none is found. Real: the Windows ``cmd`` template; the "gupkg" is a recording stub
(a batch file beside the template, and the checked-in native shim for the PATH case).
Out of scope: the interface itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TEMPLATE = SRC / "package-tui.cmd"
SHIM = SRC / "gupkg" / "shim" / "shim-console.static.exe"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher behavior")


def _package(tmp_path: Path) -> Path:
    """A package folder holding only gupkg-tui.cmd."""
    folder = tmp_path / "pkg with space"
    folder.mkdir()
    shutil.copy2(TEMPLATE, folder / "gupkg-tui.cmd")
    return folder


def _run(folder: Path, *, path: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the launcher with a controlled PATH and no pause."""
    environment = {**os.environ, "PATH": path, "GUPKG_NO_PAUSE": "1"}
    return subprocess.run(
        ["cmd", "/d", "/c", str(folder / "gupkg-tui.cmd")],
        cwd=cwd, env=environment, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )


def test_uses_the_gupkg_beside_it_and_opens_its_own_folder(tmp_path: Path) -> None:
    """A local gupkg is called with `tui <this folder>` and its exit status is returned."""
    folder = _package(tmp_path)
    (folder / "gupkg").mkdir()
    log = tmp_path / "args.log"
    (folder / "gupkg" / "gupkg.cmd").write_text(f'@echo off\n> "{log}" echo %*\nexit /b 7\n', encoding="ascii")

    result = _run(folder, path="", cwd=tmp_path)

    assert result.returncode == 7
    recorded = log.read_text(encoding="utf-8").strip()
    assert recorded.startswith("tui ") and str(folder) in recorded


def test_falls_back_to_gupkg_on_path_and_ignores_the_current_directory(tmp_path: Path) -> None:
    """gupkg.exe on PATH is used; a gupkg.exe sitting in the current directory is not."""
    folder = _package(tmp_path)
    log, decoy_log = tmp_path / "path.log", tmp_path / "decoy.log"
    for name, target_log in (("onpath", log), ("here", decoy_log)):
        probe = tmp_path / f"{name}.py"
        probe.write_text(f"import sys\nopen({target_log.as_posix()!r}, 'w').write(repr(sys.argv[1:]))\n", encoding="utf-8")
        directory = tmp_path / name
        directory.mkdir()
        shutil.copy2(SHIM, directory / "gupkg.exe")
        (directory / "gupkg.config.toml").write_text(
            f'target = "{sys.executable.replace(chr(92), "/")}"\nforward_arguments = true\n\n[[argument]]\nvalue = "{probe.as_posix()}"\n',
            encoding="utf-8",
        )

    result = _run(folder, path=str(tmp_path / "onpath"), cwd=tmp_path / "here")

    assert result.returncode == 0, result.stderr
    assert log.is_file() and not decoy_log.exists()
    assert "'tui'" in log.read_text(encoding="utf-8")


def test_without_gupkg_it_explains_how_to_install_it(tmp_path: Path) -> None:
    """No local or PATH gupkg gives a clear message and a failure status."""
    folder = _package(tmp_path)

    result = _run(folder, path="", cwd=tmp_path)

    assert result.returncode == 9009
    assert "gupkg is not installed" in result.stderr
    assert "gupkg-bootstrap.cmd" in result.stderr
