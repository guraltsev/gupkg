"""Cover how the package launcher starts gupkg: only on the Python inside its own folder.

Behavior covered: the launcher always runs the bundled interpreter in isolated
mode, never a Python found through ``GUPKG_PYTHON``, a ``gupkg.python`` file,
``python`` on ``PATH`` or the ``py`` launcher; a missing bundled interpreter stops
the launch with the reinstall message; ordinary and elevated launches start the
same interpreter with the same arguments; the bootstrap refuses to run on any
other interpreter or without ``-I``.

Real: the Windows ``cmd`` launcher, the checked-in native shim (which stands in
for ``python.exe`` and records what it was started with) and, for the bootstrap
refusal, the test interpreter. Elevation is selected with ``GUPKG_ELEVATED``,
which can only make a run stricter, because a real administrator token is not
available to tests. Out of scope: the access-control trust check (covered in
``test_elevated_runtime.py``).
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


@pytest.mark.parametrize("elevated", [False, True], ids=["ordinary", "elevated"])
def test_launch_runs_only_the_bundled_python_isolated_and_ignores_every_override(tmp_path: Path, elevated: bool) -> None:
    """Ordinary and elevated launches both run the bundled python.exe with -I -s -B and identical arguments despite every override."""
    package, log = _package(tmp_path)
    decoys, decoy_log = _decoy_python(tmp_path)
    (package / "gupkg.python").write_text(str(decoys / "python.cmd"), encoding="ascii")
    env = os.environ.copy()
    env.update(
        GUPKG_PYTHON=str(decoys / "python.cmd"),
        PYTHONPATH=str(decoys),
        PATH=f"{decoys};{env['PATH']}",
    )
    if elevated:
        env["GUPKG_ELEVATED"] = "1"

    result = _run(package, "manager", "list", env=env)

    assert result.returncode == 0, result.stderr or result.stdout
    arguments = ast.literal_eval(log.read_text(encoding="utf-8"))
    assert arguments[:3] == ["-I", "-s", "-B"]
    assert arguments[3].endswith("bootstrap.py")
    assert arguments[4:] == ["manager", "list"]
    assert not decoy_log.exists()


@pytest.mark.parametrize("elevated", [False, True], ids=["ordinary", "elevated"])
def test_launch_without_the_bundled_python_stops_with_the_reinstall_message(tmp_path: Path, elevated: bool) -> None:
    """With no python\\python.exe the launcher exits 1 telling the person to reinstall, and runs no other Python."""
    package, log = _package(tmp_path)
    (package / "python" / "python.exe").unlink()
    decoys, decoy_log = _decoy_python(tmp_path)
    env = os.environ.copy()
    env.update(GUPKG_PYTHON=str(decoys / "python.cmd"), PATH=f"{decoys};{env['PATH']}")
    if elevated:
        env["GUPKG_ELEVATED"] = "1"

    result = _run(package, "list", env=env)

    assert result.returncode == 1
    assert "bundled Python is missing" in result.stderr
    assert "Reinstall gupkg from a release ZIP" in result.stderr
    assert not decoy_log.exists() and not log.exists()


@pytest.mark.parametrize("isolated", [False, True], ids=["without -I", "with -I"])
def test_bootstrap_refuses_to_run_on_another_interpreter(tmp_path: Path, isolated: bool) -> None:
    """Running bootstrap.py on any interpreter but the bundled one exits 1 with the bundled-Python message, isolated or not."""
    command = [sys.executable, *(["-I"] if isolated else []), "-B", str(SRC / "gupkg" / "bootstrap.py"), "--version"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)

    result = subprocess.run(command, cwd=str(tmp_path), env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert "Start gupkg through gupkg.cmd or gupkg.exe" in result.stderr
    assert "runs only on its bundled Python" in result.stderr


def test_python_dash_m_gupkg_refuses_a_foreign_interpreter(tmp_path: Path) -> None:
    """`python -m gupkg` from a checkout with the test interpreter exits 1 with the bundled-Python message."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)

    result = subprocess.run(
        [sys.executable, "-B", "-m", "gupkg", "--version"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 1
    assert "runs only on its bundled Python" in result.stderr
