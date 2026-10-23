"""Dispatch the public gupkg command on the bundled interpreter.

The launcher (``gupkg.cmd``) starts this file with the Python inside the same
version directory, in isolated mode. It prepares nothing and downloads
nothing: the release ZIP already contains the interpreter, its dependencies
and the application.

Usage and API
-------------
Call ``main(...)`` from the launcher. It forwards the remaining arguments to
``gupkg.cli.main``.

Implementation Approach
-----------------------
The bootstrap makes the sibling package importable from its own location and
hands the original command arguments to the Python CLI. Every run first
verifies that the interpreter is the isolated bundled one and refuses
otherwise. An elevated run (detected from the process token) also verifies
that every directory it imports from, which includes the whole bundled
runtime, is admin-only.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from collections.abc import Sequence
from pathlib import Path

# Fixed when the module loads, so the trust check always reads the sibling file.
_ELEVATION_SOURCE = Path(__file__).resolve().parent / "elevation.py"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the public CLI on the bundled runtime and return its exit status.

    Parameters
    ----------
    argv : sequence of str, optional
        The arguments intended for ``gupkg``; ``sys.argv[1:]`` when omitted.

    Returns
    -------
    int
        The exit status returned by the public CLI.
    """
    command_args = list(sys.argv[1:] if argv is None else argv)

    # -I drops the script directory and PYTHONPATH from sys.path, so make the
    # sibling package importable from this file's own location.
    package_parent = Path(__file__).resolve().parent.parent
    if sys.path[:1] != [str(package_parent)]:
        sys.path.insert(0, str(package_parent))

    # Every run must be the isolated bundled interpreter, and an elevated run
    # must also import only admin-only code, before anything else is imported.
    elevation = _load_elevation()
    elevated = elevation.elevated_mode()
    try:
        elevation.enforce_bundled_runtime(Path(__file__).resolve().parent, elevated=elevated)
    except elevation.UntrustedLocationError as error:
        print(f"[gupkg] {error}", file=sys.stderr)
        return 1
    if elevated:
        os.environ["GUPKG_ELEVATED"] = "1"

    from gupkg.cli import main as cli_main

    return int(cli_main(command_args))


def _load_elevation() -> types.ModuleType:
    """Load the self-contained elevation module without importing the gupkg package."""
    # elevation.py has no package-relative imports, so the trust check can run
    # before any other gupkg code is imported.
    spec = importlib.util.spec_from_file_location("_gupkg_elevation", _ELEVATION_SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(main())
