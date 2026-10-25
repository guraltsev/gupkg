"""Cover the development tree's single-interpreter rule.

Behavior covered: ``scripts\\_python.cmd`` uses only the repository's ``.venv``; when it
is missing the helper first runs ``scripts\\setup.cmd`` and stops with its status if that
fails, even if a Python is on ``PATH``; ``setup.cmd`` tries uv before ``python -m venv``;
``requirements-dev.txt`` pins the runtime lock's packages; and no shipped or script file
probes for a system Python or reads an override.

Real: the Windows ``cmd`` scripts, run against a temporary copy of the repository layout,
and the checked-in files. Mocked: uv and the base Python (stand-in ``.cmd`` files that
record their arguments). No network and no package manager.
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


def _stand_in(path: Path, log: Path, name: str, status: int) -> Path:
    """Write a ``.cmd`` that appends ``<name> <arguments>`` to *log* and exits with *status*."""
    path.write_text(f'@echo off\necho {name} %*>> "{log}"\nexit /b {status}\n', encoding="ascii")
    return path


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
def test_python_helper_without_a_venv_runs_setup_first_and_then_selects_the_new_python(tmp_path: Path) -> None:
    """With no .venv the helper runs setup.cmd (here a stand-in that creates the python.exe) and selects it."""
    helper = _scripts_copy(tmp_path)
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    (helper.parent / "setup.cmd").write_text(
        f'@echo off\nmkdir "{venv_python.parent}"\ntype nul > "{venv_python}"\nexit /b 0\n', encoding="ascii"
    )

    result = _call(helper, os.environ.copy())

    assert result.returncode == 0, result.stderr
    assert "creating it with scripts\\setup.cmd" in result.stdout
    assert f'PY="{venv_python}"' in result.stdout


@windows_only
def test_python_helper_stops_with_the_status_of_a_failing_setup_even_with_python_on_path(tmp_path: Path) -> None:
    """If setup.cmd fails the helper exits with that status and selects nothing, though a Python is on PATH."""
    helper = _scripts_copy(tmp_path)
    (helper.parent / "setup.cmd").write_text("@echo off\nexit /b 7\n", encoding="ascii")
    runtime = tmp_path / "src" / "gupkg" / "python"
    runtime.mkdir(parents=True)
    shutil.copy2(sys.executable, runtime / "python.exe")
    env = os.environ.copy()
    env["PATH"] = f"{Path(sys.executable).parent};{env['PATH']}"

    result = _call(helper, env)

    assert result.returncode == 7
    assert "PY=" not in result.stdout


@windows_only
def test_setup_tries_uv_with_seed_first_and_falls_back_to_python_m_venv(tmp_path: Path) -> None:
    """With no .venv, setup runs ``uv venv --seed`` and, when uv fails, ``<python> -m venv``; both failing is an error."""
    helper = _scripts_copy(tmp_path)
    shutil.copy2(ROOT / "scripts" / "setup.cmd", helper.parent / "setup.cmd")
    (tmp_path / "requirements-dev.txt").write_text("", encoding="utf-8")
    log = tmp_path / "calls.log"
    tools = tmp_path / "tools-on-path"
    tools.mkdir()
    _stand_in(tools / "uv.cmd", log, "uv", 1)
    base = _stand_in(tmp_path / "base.cmd", log, "base", 1)
    env = os.environ.copy()
    env["PATH"] = f"{tools};{env['PATH']}"

    result = subprocess.run([str(helper.parent / "setup.cmd"), str(base)], env=env, capture_output=True, text=True, check=False)

    calls = log.read_text(encoding="utf-8").splitlines()
    assert result.returncode != 0
    assert calls[0].startswith("uv venv --seed --python ")
    assert calls[1].startswith("base -m venv ")
    assert "Could not create the environment" in result.stderr


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
