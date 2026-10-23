"""Cover the development tree's single-interpreter rule.

Behavior covered: ``scripts\\_python.cmd`` uses only the repository's ``.venv`` and
exits 9009 with the creation command when it is missing, even if a Python is on
``PATH``; ``requirements-dev.txt`` pins the runtime lock's packages; and no
shipped or script file probes for a system Python or reads an override.

Real: the Windows ``cmd`` helper script, run against a temporary copy of the
repository layout, and the checked-in files. No network and no package manager.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("py -3", "call python", "GUPKG_PYTHON", "gupkg.python", "GUPKG_EMBEDDED", "GUPKG_BUNDLED_RUNTIME", "--embedded")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows batch scripts")


def _scripts_copy(root: Path) -> Path:
    """Copy ``_python.cmd`` into ``<root>\\scripts`` so REPO_ROOT resolves to a controlled folder."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "_python.cmd", root / "scripts" / "_python.cmd")
    return root / "scripts" / "_python.cmd"


def _call(helper: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the helper and print the PY it selected."""
    wrapper = helper.with_name("show_py.cmd")
    wrapper.write_text(f'@echo off\ncall "{helper}"\nif errorlevel 1 exit /b %ERRORLEVEL%\necho PY=%PY%\n', encoding="ascii")
    return subprocess.run([str(wrapper)], env=env, capture_output=True, text=True, check=False)


def _pins(path: Path) -> dict[str, str]:
    """Return ``name -> version`` for every ``name==version`` line of a requirements file, without pip."""
    pins = {
        match.group(1).lower(): match.group(2)
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := re.match(r"([A-Za-z0-9_.-]+)==(\S+)", line))
    }
    pins.pop("pip", None)
    return pins


@windows_only
def test_python_helper_without_a_venv_exits_9009_even_with_python_on_path(tmp_path: Path) -> None:
    """With no .venv the helper exits 9009 and names the creation command, though python and the committed runtime exist."""
    helper = _scripts_copy(tmp_path)
    runtime = tmp_path / "src" / "gupkg" / "python"
    runtime.mkdir(parents=True)
    shutil.copy2(sys.executable, runtime / "python.exe")
    env = os.environ.copy()
    env["PATH"] = f"{Path(sys.executable).parent};{env['PATH']}"

    result = _call(helper, env)

    assert result.returncode == 9009
    assert "PY=" not in result.stdout
    assert "-m venv .venv" in result.stderr and "requirements-dev.txt" in result.stderr


@windows_only
def test_python_helper_selects_the_venv_interpreter(tmp_path: Path) -> None:
    """With a .venv the helper selects exactly its python.exe."""
    helper = _scripts_copy(tmp_path)
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()

    result = _call(helper, os.environ.copy())

    assert result.returncode == 0, result.stderr
    assert f'PY="{venv_python}"' in result.stdout


def test_requirements_dev_pins_the_runtime_lock_packages_and_adds_pytest() -> None:
    """requirements-dev.txt holds pytest and the lock's runtime packages at the locked versions (pip itself excluded)."""
    requirements = ROOT / "requirements-dev.txt"

    assert _pins(requirements) == _pins(ROOT / "tools" / "gupkg-deps.lock")
    assert "pytest" in [line.strip() for line in requirements.read_text(encoding="utf-8").splitlines()]


def test_shipped_code_and_scripts_never_probe_for_a_system_python() -> None:
    """No file under src/ (outside the bundled runtime), scripts/ or tools/ mentions an interpreter probe or override."""
    offenders = []
    for folder in ("src", "scripts", "tools"):
        for path in (ROOT / folder).rglob("*"):
            relative = path.relative_to(ROOT)
            if not path.is_file() or relative.parts[:3] == ("src", "gupkg", "python") or "__pycache__" in relative.parts:
                continue
            if path.suffix.lower() not in {".py", ".cmd", ".ps1", ".toml", ".md", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            offenders += [f"{relative}: {word}" for word in FORBIDDEN if word in text]

    assert offenders == []
