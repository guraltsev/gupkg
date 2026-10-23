"""Cover how the package launcher starts gupkg: only on the Python inside its own folder.

Behavior covered: the launcher always runs the bundled interpreter in isolated
mode, never a Python found through ``GUPKG_PYTHON``, a ``gupkg.python`` file,
``python`` on ``PATH`` or the ``py`` launcher; a missing bundled interpreter stops
the launch with the reinstall message; an elevated launch additionally passes
the elevation flag.

Real: the Windows ``cmd`` launcher and the checked-in native shim, which stands
in for ``python.exe`` and records what it was started with. The elevated branch
is selected with the launcher's strictness-only switch ``GUPKG_FORCE_ELEVATED``
because a real administrator token is not available to tests. Out of scope: the
Python-side trust check (covered in ``test_elevated_runtime.py``).
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
SHIM = SRC / "gupkg" / "shim" / "shim-console.static.exe"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher behavior")


def _package(root: Path) -> tuple[Path, Path]:
    """Create a package whose bundled python.exe records its arguments and exits 0."""
    package = root / "gupkg"
    (package / "python").mkdir(parents=True)
    shutil.copy2(SRC / "gupkg" / "gupkg.cmd", package / "gupkg.cmd")
    shutil.copy2(SRC / "gupkg" / "bootstrap.py", package / "bootstrap.py")
    log = root / "bundled.log"
    probe = root / "bundled_probe.py"
    probe.write_text(
        f"import sys\nopen({log.as_posix()!r}, 'w', encoding='utf-8').write(repr(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    python = package / "python" / "python.exe"
    shutil.copy2(SHIM, python)
    python.with_suffix(".config.toml").write_text(
        f'target = "{sys.executable.replace(chr(92), "/")}"\nforward_arguments = true\n\n'
        f'[[argument]]\nvalue = "{probe.as_posix()}"\n',
        encoding="utf-8",
    )
    return package, log


def _decoy_python(root: Path) -> tuple[Path, Path]:
    """Create a fake ``python`` command that records that it ran, for PATH and override tests."""
    folder = root / "decoy-bin"
    folder.mkdir()
    ran = root / "decoy.log"
    for name in ("python.cmd", "py.cmd"):
        (folder / name).write_text(f'@echo off\necho ran> "{ran}"\n', encoding="ascii")
    return folder, ran


def _run(package: Path, *arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the launcher through cmd.exe from the package's parent directory."""
    return subprocess.run(
        ["cmd", "/d", "/c", str(package / "gupkg.cmd"), *arguments],
        cwd=str(package.parent),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_launch_runs_only_the_bundled_python_isolated_and_ignores_every_override(tmp_path: Path) -> None:
    """A launch uses -I -s -B on the bundled interpreter even with GUPKG_PYTHON, gupkg.python and PATH Pythons set."""
    package, log = _package(tmp_path)
    decoys, decoy_log = _decoy_python(tmp_path)
    (package / "gupkg.python").write_text(str(decoys / "python.cmd"), encoding="ascii")
    env = os.environ.copy()
    env.pop("GUPKG_FORCE_ELEVATED", None)
    env.update(GUPKG_PYTHON=str(decoys / "python.cmd"), PATH=f"{decoys};{env['PATH']}")

    result = _run(package, "manager", "list", env=env)

    assert result.returncode == 0, result.stderr or result.stdout
    arguments = ast.literal_eval(log.read_text(encoding="utf-8"))
    assert arguments[:3] == ["-I", "-s", "-B"]
    assert arguments[3].endswith("bootstrap.py")
    assert arguments[4:] in (["manager", "list"], ["--elevated", "manager", "list"])
    assert not decoy_log.exists()


def test_elevated_launch_passes_the_elevation_flag(tmp_path: Path) -> None:
    """GUPKG_FORCE_ELEVATED makes the launch strict: the bootstrap is told with --elevated."""
    package, log = _package(tmp_path)
    env = os.environ.copy()
    env["GUPKG_FORCE_ELEVATED"] = "1"

    result = _run(package, "manager", "list", env=env)

    assert result.returncode == 0, result.stderr or result.stdout
    arguments = ast.literal_eval(log.read_text(encoding="utf-8"))
    assert arguments[:3] == ["-I", "-s", "-B"]
    assert arguments[4:] == ["--elevated", "manager", "list"]


@pytest.mark.parametrize("elevated", [False, True], ids=["ordinary", "elevated"])
def test_launch_without_the_bundled_python_stops_with_the_reinstall_message(tmp_path: Path, elevated: bool) -> None:
    """With no python\\python.exe the launcher exits 1 telling the person to reinstall, and runs no other Python."""
    package, log = _package(tmp_path)
    (package / "python" / "python.exe").unlink()
    decoys, decoy_log = _decoy_python(tmp_path)
    env = os.environ.copy()
    env.pop("GUPKG_FORCE_ELEVATED", None)
    env.update(GUPKG_PYTHON=str(decoys / "python.cmd"), PATH=f"{decoys};{env['PATH']}")
    if elevated:
        env["GUPKG_FORCE_ELEVATED"] = "1"

    result = _run(package, "list", env=env)

    assert result.returncode == 1
    assert "bundled Python is missing" in result.stderr
    assert "Reinstall gupkg from a release ZIP" in result.stderr
    assert not decoy_log.exists() and not log.exists()
