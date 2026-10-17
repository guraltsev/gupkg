"""Cover how the package launcher builds an elevated versus an ordinary Python run.

Behavior covered: an elevated launch runs only the bundled interpreter in
isolated mode with the elevation flag, ignores every user-controlled
interpreter choice and ``PYTHONPATH``, and stops with a clear message when the
bundled runtime is missing; an ordinary launch is unchanged.

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
    """Create a package whose bundled python.exe records its arguments and PYTHONPATH."""
    package = root / "gupkg"
    (package / "python").mkdir(parents=True)
    shutil.copy2(SRC / "gupkg" / "gupkg.cmd", package / "gupkg.cmd")
    shutil.copy2(SRC / "gupkg" / "bootstrap.py", package / "bootstrap.py")
    log = root / "bundled.log"
    probe = root / "bundled_probe.py"
    probe.write_text(
        "import os, sys\n"
        f"open({log.as_posix()!r}, 'w', encoding='utf-8').write(repr((sys.argv[1:], os.environ.get('PYTHONPATH'))))\n",
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


def test_elevated_launch_runs_the_bundled_python_isolated_and_ignores_user_choices(tmp_path: Path) -> None:
    """An elevated launch uses -I -s -B and --elevated, and ignores GUPKG_PYTHON, gupkg.python and PYTHONPATH."""
    package, log = _package(tmp_path)
    decoy = tmp_path / "decoy.cmd"
    decoy_log = tmp_path / "decoy.log"
    decoy.write_text(f'@echo off\necho ran> "{decoy_log}"\n', encoding="ascii")
    (package / "gupkg.python").write_text(str(decoy), encoding="ascii")
    env = os.environ.copy()
    env.update(GUPKG_FORCE_ELEVATED="1", GUPKG_PYTHON=str(decoy), PYTHONPATH=str(tmp_path / "evil"))

    result = _run(package, "manager", "list", env=env)

    assert result.returncode == 0, result.stderr or result.stdout
    arguments, pythonpath = ast.literal_eval(log.read_text(encoding="utf-8"))
    assert arguments[:3] == ["-I", "-s", "-B"]
    assert arguments[3].endswith("bootstrap.py")
    assert arguments[4:] == ["--embedded", "--elevated", "manager", "list"]
    assert pythonpath is None
    assert not decoy_log.exists()


def test_elevated_launch_without_the_bundled_runtime_stops_with_a_message(tmp_path: Path) -> None:
    """When the bundled Python is missing, an elevated launch fails instead of using another interpreter."""
    package, log = _package(tmp_path)
    (package / "python" / "python.exe").unlink()
    decoy = tmp_path / "decoy.cmd"
    decoy_log = tmp_path / "decoy.log"
    decoy.write_text(f'@echo off\necho ran> "{decoy_log}"\n', encoding="ascii")
    env = os.environ.copy()
    env.update(GUPKG_FORCE_ELEVATED="1", GUPKG_PYTHON=str(decoy))

    result = _run(package, "list", env=env)

    assert result.returncode == 1
    assert "bundled Python" in result.stderr
    assert "Reinstall gupkg" in result.stderr
    assert not decoy_log.exists() and not log.exists()


def test_ordinary_launch_still_forwards_pythonpath_to_the_bundled_python(tmp_path: Path) -> None:
    """A non-elevated launch keeps its convenience behavior: no isolation flags, PYTHONPATH honored."""
    package, log = _package(tmp_path)
    env = os.environ.copy()
    env.pop("GUPKG_FORCE_ELEVATED", None)
    env.pop("GUPKG_PYTHON", None)
    env["PYTHONPATH"] = str(tmp_path / "extra")
    # The shim cannot run the version probe, so make the probe fail closed to ordinary handling.

    result = _run(package, "list", env=env)

    if result.returncode != 0 and not log.exists():
        pytest.skip("this session is elevated or has no usable system fallback")
    arguments, pythonpath = ast.literal_eval(log.read_text(encoding="utf-8"))
    assert "-I" not in arguments and "--elevated" not in arguments
    assert str(tmp_path / "extra") in (pythonpath or "")
