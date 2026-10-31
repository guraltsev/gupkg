"""Copy the package launchers: ``install.cmd`` and ``manage.cmd`` of a version directory, ``check-updates.cmd`` of a tracker.

Every launcher is a finished, plain ``cmd`` file kept in the ``templates`` folder of the gupkg tree (``src\\templates`` in the
source tree, ``<tree>\\templates`` in a release) and copied byte for byte by whichever gupkg runs the operation (``check-updates``,
``pkg new``, ``pkg launchers``, ``pkg adopt``, ``pkg bundle``, ``pkg export``, and the registry tool all call this module). A
launcher lives at ``<root>\\<name>\\<version>\\`` (or ``<root>\\<name>\\_check_updates\\``) and finds a gupkg by its own position,
in this order: the gupkg bundled in its folder, the folder named by the first line of ``<root>\\.gupkg\\gupkg-location`` (read as
data, never executed), then the first ``gupkg.cmd`` or ``gupkg.exe`` on ``PATH``; with none of them it exits 9009 with the way to
get gupkg. Exit code 6, which a gupkg returns for a package it cannot handle, moves on to the next step. The registry, AppData,
ProgramData and the current directory are never searched. An elevated launcher first checks, with System32's PowerShell, that
everything it will run is admin-only (on ``PATH`` it skips and names every entry that is not) and refuses otherwise (design
section 21.3, decisions D30 and D52).

Usage and API
-------------
``launcher_text(command)`` returns the text of one launcher (``install``, ``tui`` or ``check-updates``) with Windows line endings.
``write_launchers(directory, names)`` writes launchers into a version directory (both by default) or tracker folder, replacing
existing files. ``VERSION_LAUNCHERS`` and ``TRACKER_LAUNCHER`` name the files each kind of folder carries.

Implementation Approach
-----------------------
The files are not generated: a launcher is byte-identical whichever gupkg wrote it, and a registry fingerprint can cover it. The
``tui`` command is the launcher named ``manage.cmd``; the name ``gupkg-tui.cmd`` belongs to the program of a gupkg tree.
"""

from __future__ import annotations

from pathlib import Path

# The launchers of a version directory, with the gupkg command each one runs.
VERSION_LAUNCHERS = {"install.cmd": "install", "manage.cmd": "tui"}

# The one launcher of a ``_check_updates`` folder: it only checks, and never installs.
TRACKER_LAUNCHER = "check-updates.cmd"

# Every launcher name and its command.
LAUNCHERS = {**VERSION_LAUNCHERS, TRACKER_LAUNCHER: "check-updates"}

# The folder of the finished launchers: beside the ``gupkg`` package in the source tree and in a release tree.
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"

_FILE_OF_COMMAND = {command: name for name, command in LAUNCHERS.items()}


def launcher_text(command: str) -> str:
    """Return the text of one launcher.

    Parameters
    ----------
    command : str
        ``"install"`` for ``install.cmd``, ``"tui"`` for ``manage.cmd`` or ``"check-updates"`` for ``check-updates.cmd``.

    Returns
    -------
    str
        The launcher with Windows line endings.

    Raises
    ------
    ValueError
        If *command* is not a launcher command.
    """
    if command not in _FILE_OF_COMMAND:
        raise ValueError(f"unknown launcher command: {command}")
    text = (TEMPLATES_DIR / _FILE_OF_COMMAND[command]).read_bytes().decode("ascii")
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def write_launchers(directory: Path, names: tuple[str, ...] | None = None) -> list[Path]:
    """Write launchers into *directory*, replacing existing ones.

    Parameters
    ----------
    directory : Path
        The version directory (or the ``_check_updates`` folder).
    names : tuple of str, optional
        File names to write; ``install.cmd`` and ``manage.cmd`` by default.

    Returns
    -------
    list of Path
        The files written.

    Raises
    ------
    ValueError
        If a name is not a launcher.
    """
    written = []
    for name in names if names is not None else tuple(VERSION_LAUNCHERS):
        if name not in LAUNCHERS:
            raise ValueError(f"unknown launcher: {name}")
        path = Path(directory) / name
        path.write_bytes(launcher_text(LAUNCHERS[name]).encode("ascii"))
        written.append(path)
    return written
