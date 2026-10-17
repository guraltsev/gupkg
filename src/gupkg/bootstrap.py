"""Prepare an embedded runtime and dispatch the public gupkg command.

The bootstrap entry point is used after batch code has selected an interpreter.
It owns Python-readable runtime support files and the embedded-runtime pip
bootstrap, while ordinary interpreter selection remains in the local command
file.

Usage and API
-------------
Call ``main(...)`` from the internal launcher. It prepares an embedded runtime
when requested and forwards the remaining arguments to ``gupkg.cli.main``.

Implementation Approach
-----------------------
The bootstrap establishes import paths from its own package location, writes
the small runtime support files needed by an embeddable interpreter, and then
hands the original command arguments to the existing Python CLI. System Python
invocations skip embedded-runtime mutation. An elevated run (requested with
``--elevated`` or detected from the process token) mutates and downloads
nothing: it first verifies that the interpreter is the isolated bundled one and
that every directory it imports from is admin-only, and refuses otherwise.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import types
from collections.abc import Sequence
from urllib.request import urlopen

# Fixed when the module loads, so the trust check always reads the sibling file.
_ELEVATION_SOURCE = Path(__file__).resolve().parent / "elevation.py"


def main(argv: Sequence[str] | None = None) -> int:
    """Prepare the selected runtime and return the public CLI exit status.

    Parameters
    ----------
    argv : sequence of str, optional
        Bootstrap options followed by the arguments intended for ``gupkg``.

    Returns
    -------
    int
        The exit status returned by the public CLI.

    Raises
    ------
    RuntimeError
        If embedded-runtime support files or pip cannot be prepared.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--embedded", action="store_true")
    parser.add_argument("--elevated", action="store_true")
    options, command_args = parser.parse_known_args(argv)

    # Make the sibling package importable regardless of the caller's cwd.
    package_parent = Path(__file__).resolve().parent.parent
    if sys.path[:1] != [str(package_parent)]:
        sys.path.insert(0, str(package_parent))

    # An elevated process, whether the launcher said so or the token shows it,
    # must pass the trust check before anything else is imported or changed.
    elevation = _load_elevation()
    if options.elevated or elevation.is_elevated():
        try:
            elevation.enforce_elevated_runtime(Path(__file__).resolve().parent)
        except elevation.UntrustedLocationError as error:
            print(f"[gupkg] {error}", file=sys.stderr)
            return 1
        os.environ["GUPKG_ELEVATED"] = "1"
        os.environ["GUPKG_BUNDLED_RUNTIME"] = "1"
    elif options.embedded:
        # The bootstrap file and bundled runtime are one owned unit. Never
        # derive a write target from the interpreter that launched the command.
        runtime_directory = Path(__file__).resolve().parent / "python"
        _prepare_embedded_runtime(runtime_directory)
        os.environ["GUPKG_BUNDLED_RUNTIME"] = "1"

    # Keep bootstrap-only flags out of the public command parser.
    from gupkg.cli import main as cli_main

    return int(cli_main(list(command_args)))


def _load_elevation() -> types.ModuleType:
    """Load the self-contained elevation module without importing the gupkg package."""
    # elevation.py has no package-relative imports, so the trust check can run
    # before any other gupkg code is imported.
    spec = importlib.util.spec_from_file_location("_gupkg_elevation", _ELEVATION_SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _prepare_embedded_runtime(runtime_directory: Path) -> None:
    """Write embedded-runtime support files and ensure pip is available."""
    # Keep the bundled interpreter isolated while making its own dependency
    # directory importable. Every generated path remains inside this runtime.
    pth_name = f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    pth_path = runtime_directory / pth_name
    content = f"python{sys.version_info.major}{sys.version_info.minor}.zip\n.\n..\nLib/site-packages\n\nimport site\n"

    # A release bundle ships this file already; rewrite it only when it differs,
    # and tolerate an admin-only install that this user cannot write.
    try:
        if not pth_path.is_file() or pth_path.read_text(encoding="ascii") != content:
            pth_path.write_text(content, encoding="ascii")
    except OSError:
        pass
    _ensure_pip(runtime_directory)


def _ensure_pip(runtime_directory: Path) -> None:
    """Install pip into the embedded runtime's mutable dependency location."""
    runtime_python = runtime_directory / "python.exe"

    # Invoke only the bundled executable, even when another launcher reached
    # this bootstrap command.
    probe = subprocess.run(
        [runtime_python, "-m", "pip", "--version"],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return

    # Keep the installer and every installed file inside the local bundle.
    pip_target = runtime_directory / "Lib" / "site-packages"
    pip_target.mkdir(parents=True, exist_ok=True)
    installer_path = runtime_directory / "get-pip.py"
    try:
        with urlopen("https://bootstrap.pypa.io/get-pip.py") as response:
            installer_path.write_bytes(response.read())
        environment = os.environ.copy()
        environment["GUPKG_BOOTSTRAPPING_PIP"] = "1"
        environment["PIP_TARGET"] = str(pip_target)
        result = subprocess.run(
            [runtime_python, str(installer_path), "--no-warn-script-location"],
            env=environment,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Could not install pip for embedded Python.")
    finally:
        installer_path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"[gupkg] {error}", file=sys.stderr)
        raise SystemExit(1)
