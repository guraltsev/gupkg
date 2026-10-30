"""Cover how the package launcher starts gupkg: only on the Python inside its own folder.

Behavior covered: the launcher always runs the bundled interpreter in isolated
mode, never a Python found through ``GUPKG_PYTHON``, a ``gupkg.python`` file,
``python`` on ``PATH`` or the ``py`` launcher; a missing bundled interpreter stops
the launch with the reinstall message; ordinary and elevated launches start the
same interpreter with the same arguments; the bootstrap refuses to run on any
other interpreter or without ``-I``.

Real: the Windows ``cmd`` launcher, the checked-in native shim (which stands in
for ``python.exe`` and records what it was started with) and, for the bootstrap
refusal, the test interpreter. The launcher has no elevated branch (the bootstrap detects
elevation), so ``GUPKG_ELEVATED`` is set only to show it changes nothing here. Out of scope: the access-control trust check (covered in
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


@pytest.mark.parametrize("marker", [None, "1"], ids=["no-marker", "GUPKG_ELEVATED=1"])
def test_launch_runs_only_the_bundled_python_isolated_and_ignores_every_override(tmp_path: Path, marker: str | None) -> None:
    """The launcher runs the bundled python.exe with -I -s -B -S and the same arguments despite every override and whatever GUPKG_ELEVATED says.

    The launcher has no elevated branch; elevation is detected in the bootstrap
    (see test_elevated_runtime.py), so an elevated run starts this same command line.
    """
    package, log = _package(tmp_path)
    decoys, decoy_log = _decoy_python(tmp_path)
    (package / "gupkg.python").write_text(str(decoys / "python.cmd"), encoding="ascii")
    env = os.environ.copy()
    env.update(
        GUPKG_PYTHON=str(decoys / "python.cmd"),
        PYTHONPATH=str(decoys),
        PATH=f"{decoys};{env['PATH']}",
    )
    if marker:
        env["GUPKG_ELEVATED"] = marker

    result = _run(package, "manager", "list", env=env)

    assert result.returncode == 0, result.stderr or result.stdout
    arguments = ast.literal_eval(log.read_text(encoding="utf-8"))
    assert arguments[:4] == ["-I", "-s", "-B", "-S"]
    assert arguments[4].endswith("bootstrap.py")
    assert arguments[5:] == ["manager", "list"]
    assert not decoy_log.exists()


@pytest.mark.parametrize("marker", [None, "1"], ids=["no-marker", "GUPKG_ELEVATED=1"])
def test_launch_without_the_bundled_python_stops_with_the_reinstall_message(tmp_path: Path, marker: str | None) -> None:
    """With no python\\python.exe the launcher exits 1 telling the person to reinstall, and runs no other Python."""
    package, log = _package(tmp_path)
    (package / "python" / "python.exe").unlink()
    decoys, decoy_log = _decoy_python(tmp_path)
    env = os.environ.copy()
    env.update(GUPKG_PYTHON=str(decoys / "python.cmd"), PATH=f"{decoys};{env['PATH']}")
    if marker:
        env["GUPKG_ELEVATED"] = marker

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


def _real_layout(root: Path) -> tuple[Path, Path]:
    """Copy the committed bundled runtime and gupkg code into ``<root>/gupkg`` and return it with a marker path."""
    package = root / "gupkg"
    shutil.copytree(SRC / "gupkg", package, ignore=shutil.ignore_patterns("__pycache__", "shim", "7zip", "*.md"))
    marker = root / "planted-ran.txt"
    site_packages = package / "python" / "Lib" / "site-packages"
    for folder in (package, package / "python", site_packages):
        (folder / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'a').write('sitecustomize in {folder.name}\n')\n", encoding="utf-8")
        (folder / "usercustomize.py").write_text(f"open({str(marker)!r}, 'a').write('usercustomize in {folder.name}\n')\n", encoding="utf-8")
    (site_packages / "planted.pth").write_text(f"import sys; open({str(marker)!r}, 'a').write('pth')\n", encoding="utf-8")
    return package, marker


def test_nothing_planted_beside_the_bundled_python_runs_before_the_checks(tmp_path: Path) -> None:
    """On the real bundled python.exe a planted sitecustomize, usercustomize or .pth never runs, in an ordinary or an elevated-marked launch."""
    package, marker = _real_layout(tmp_path)
    environment = os.environ.copy()

    ordinary = _run(package, "--version", env=environment)
    environment["GUPKG_ELEVATED"] = "1"
    elevated = _run(package, "--version", env=environment)

    assert ordinary.returncode == 0, ordinary.stderr
    assert ordinary.stdout.startswith("gupkg ") and ordinary.stdout.count("\n") == 1
    assert elevated.returncode == 1, elevated.stdout
    assert "[gupkg]" in elevated.stderr
    assert not marker.exists()


def test_a_module_planted_in_the_package_parent_never_runs_before_the_elevated_check_refuses(tmp_path: Path) -> None:
    """With a ctypes.py and struct.py planted beside the version folder, an elevated-marked real launch refuses without running them."""
    package, marker = _real_layout(tmp_path)
    for name in ("ctypes", "struct", "dataclasses"):
        (tmp_path / f"{name}.py").write_text(f"open({str(marker)!r}, 'a').write({name!r})\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["GUPKG_ELEVATED"] = "1"

    result = _run(package, "--version", env=environment)

    assert result.returncode == 1, result.stdout
    assert not marker.exists()


# ---------------------------------------------------------------------------
# The arguments reach Python exactly as the caller passed them (issue 035, T18)
# ---------------------------------------------------------------------------

# (id, the argument text as a person types it after the launcher name, the arguments Python must receive).
FORWARDED = [
    ("equals-and-comma", "--overwrite=a,b", ["--overwrite=a,b"]),
    ("equals", "--skip-checksum=cli", ["--skip-checksum=cli"]),
    ("equals-in-quotes", '"--root=C:\\a b\\c"', ["--root=C:\\a b\\c"]),
    ("semicolon", "--x=1;2", ["--x=1;2"]),
    ("empty-item", "--overwrite=a,,b", ["--overwrite=a,,b"]),
    ("empty-argument", '""', [""]),
    ("empty-between", 'a "" b', ["a", "", "b"]),
    ("spaces", '"a b  c"', ["a b  c"]),
    ("trailing-backslash", '"C:\\x y\\\\"', ["C:\\x y\\"]),
    ("non-ascii", '"caf\u00e9 \u0436\u0443\u043a"', ["caf\u00e9 \u0436\u0443\u043a"]),
    ("long", "x" * 100, ["x" * 100]),
    ("double-dash", "-- target", ["--", "target"]),
    ("several", "install tool --yes", ["install", "tool", "--yes"]),
]

# Hostile text inside quotes: it must arrive as text and never run (the canary file is what a command would create).
HOSTILE = [
    ("ampersand", '"a&b"', ["a&b"]),
    ("ampersand-command", '"a&copy nul %CANARY%"', ["a&copy nul %CANARY%"]),
    ("pipe", '"a|b"', ["a|b"]),
    ("pipe-command", '"a|copy nul %CANARY%"', ["a|copy nul %CANARY%"]),
    ("redirection", '"a>%CANARY%"', ["a>%CANARY%"]),
    ("quoted-caret", '"a^b"', ["a^b"]),
    ("expanded-variable", '"%VAR%"', ["EXPANDED"]),
    ("delayed-expansion", "!VAR!", ["!VAR!"]),
    ("doubled-quote", '"a""b"', ['a"b']),
    ("unbalanced-quote", '"a&copy nul %CANARY%', ["a&copy nul %CANARY%"]),
]


def _forwarding_layout(root: Path) -> Path:
    """Create ``<root>/gupkg.cmd`` (outer) and ``<root>/gupkg`` (inner, with the manager and tui launchers); return ``<root>``."""
    package, _log = _package(root)
    for name in ("gupkg-manager.cmd", "gupkg-tui.cmd"):
        shutil.copy2(SRC / "gupkg" / name, package / name)
    for name in ("gupkg.cmd", "gupkg-manager.cmd", "gupkg-tui.cmd"):
        shutil.copy2(SRC / name, root / name)
    return root


def _typed(launcher: Path, raw: str, root: Path, *, canary: Path) -> list[str]:
    """Run *launcher* with *raw* as the typed argument text through cmd.exe and return the arguments the bundled Python received."""
    env = os.environ.copy()
    env.update(VAR="EXPANDED", CANARY=str(canary))
    log = root / "bundled.log"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        f'"{os.environ["ComSpec"]}" /d /c ""{launcher}" {raw}"', cwd=str(root), env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    arguments = ast.literal_eval(log.read_text(encoding="utf-8"))
    assert arguments[:4] == ["-I", "-s", "-B", "-S"] and arguments[4].endswith("bootstrap.py")
    return arguments[5:]


@pytest.mark.parametrize("entry", ["inner", "outer"])
@pytest.mark.parametrize(("raw", "expected"), [case[1:] for case in FORWARDED], ids=[case[0] for case in FORWARDED])
def test_gupkg_cmd_hands_python_exactly_the_arguments_it_was_given(tmp_path: Path, entry: str, raw: str, expected: list[str]) -> None:
    """`=`, `,` and `;` stay inside an argument, an empty argument stays an argument, spaces, quotes, a trailing backslash, non-ASCII and 100 characters survive.

    Both the launcher in the version directory and the `gupkg.cmd` beside it (the target of the shims) are run.
    """
    root = _forwarding_layout(tmp_path)
    launcher = root / "gupkg" / "gupkg.cmd" if entry == "inner" else root / "gupkg.cmd"

    assert _typed(launcher, raw, root, canary=tmp_path / "canary.txt") == expected


@pytest.mark.parametrize("entry", ["inner", "outer"])
@pytest.mark.parametrize(("raw", "expected"), [case[1:] for case in HOSTILE], ids=[case[0] for case in HOSTILE])
def test_gupkg_cmd_never_runs_what_a_quoted_argument_contains(tmp_path: Path, entry: str, raw: str, expected: list[str]) -> None:
    """`& | > ^ ! "` and a `copy` command inside quotes arrive as text and no command runs (no canary file appears)."""
    root = _forwarding_layout(tmp_path)
    launcher = root / "gupkg" / "gupkg.cmd" if entry == "inner" else root / "gupkg.cmd"
    canary = tmp_path / "canary.txt"

    received = _typed(launcher, raw, root, canary=canary)

    assert received == [item.replace("%CANARY%", str(canary)) for item in expected]
    assert not canary.exists()


@pytest.mark.parametrize("entry", ["inner", "outer"])
@pytest.mark.parametrize(("name", "program"), [("gupkg-manager.cmd", "--gupkg-program=manager"), ("gupkg-tui.cmd", "--gupkg-program=tui")])
def test_the_manager_and_tui_launchers_forward_every_argument_unchanged_after_the_program_name(tmp_path: Path, entry: str, name: str, program: str) -> None:
    """The program flag comes first, then the caller's arguments exactly as typed (all the cases at once)."""
    root = _forwarding_layout(tmp_path)
    launcher = root / "gupkg" / name if entry == "inner" else root / name
    raw = " ".join(case[1] for case in FORWARDED)
    expected = [item for case in FORWARDED for item in case[2]]

    assert _typed(launcher, raw, root, canary=tmp_path / "canary.txt") == [program, *expected]


def test_the_real_gupkg_reads_equals_and_empty_items_through_gupkg_cmd(tmp_path: Path) -> None:
    """On the real bundled Python, `--skip-checksum=bogus` and `--overwrite=a,,b` are judged as written (usage errors, exit 2), not as split words."""
    package, _marker = _real_layout(tmp_path)
    environment = os.environ.copy()
    environment.update(USERPROFILE=str(tmp_path), LOCALAPPDATA=str(tmp_path), APPDATA=str(tmp_path))

    checksum = _run(package, "install", "tool", "--skip-checksum=bogus", env=environment)
    items = _run(package, "install", "tool", "--overwrite=orphaned-bin-files,,orphaned-shortcuts", env=environment)

    assert checksum.returncode == 2 and "--skip-checksum=bogus is not understood" in checksum.stderr, checksum.stderr
    assert items.returncode == 2 and "empty item" in items.stderr and "orphaned-bin-files,,orphaned-shortcuts" in items.stderr, items.stderr
