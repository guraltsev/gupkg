"""Recognise 0.17 hook code that a built-in service now replaces.

``scan_hooks(directory)`` reads the Python files of a package's ``pkg.local`` and returns a
``HookFinding`` for every known service pattern with the declarative replacement to use. The
scan only reports: it never edits or deletes a hook.

The patterns live in core (``gupkg.services.hints``), so ``gupkg pkg check`` and this tool
give the same advice; this module only keeps the names the converter already uses.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.services.hints import Hint, scan_directory

# A finding is a core hint: the hook file, the service that replaces it, and the format 2 text.
HookFinding = Hint


def scan_hooks(directory: Path) -> list[HookFinding]:
    """Report the known service patterns in a package directory's hooks.

    Parameters
    ----------
    directory : Path
        The directory that holds ``pkg.local``.

    Returns
    -------
    list of HookFinding
        One finding per matching pattern and file, in file and pattern order. An
        unreadable file is skipped.
    """
    return scan_directory(directory)
