"""Generate the package launchers: ``install.cmd`` and ``gupkg-tui.cmd`` of a version directory, ``check-updates.cmd`` of a tracker.

Every launcher is the same plain ``cmd`` file, written by whichever gupkg runs the operation (``check-updates``, ``pkg new``,
``pkg bundle``, ``pkg export``, and the registry tool all call this module). A launcher lives at
``<root>\\<name>\\<version>\\`` (or ``<root>\\<name>\\_check_updates\\``) and finds a gupkg by its own position, in this order: the
gupkg bundled in its folder, the folder named by the first line of ``<root>\\.gupkg\\gupkg-location`` (read as data, never
executed), then the first ``gupkg.cmd`` or ``gupkg.exe`` on ``PATH``; with none of them it exits 9009 with the way to get gupkg.
Exit code 6, which a gupkg returns for a package it cannot handle, moves on to the next step. The registry, AppData, ProgramData
and the current directory are never searched. An elevated launcher first checks, with System32's PowerShell, that everything it
will run is admin-only (on ``PATH`` it skips and names every entry that is not) and refuses otherwise (design section 21.3,
decisions D30 and D52).

Usage and API
-------------
``launcher_text(command)`` returns the text of one launcher (``install``, ``tui`` or ``check-updates``) with Windows line endings.
``write_launchers(directory, names)`` writes launchers into a version directory (both by default) or tracker folder, replacing
existing files. ``VERSION_LAUNCHERS`` and ``TRACKER_LAUNCHER`` name the files each kind of folder carries.

Implementation Approach
-----------------------
The text comes from ``package-launcher.cmd`` beside this module; only the title, the command line and, for ``check-updates``, the
line that derives the package name from the folder's parent are filled in. Nothing else varies, so a launcher is byte-identical
whichever gupkg wrote it, and a registry fingerprint can cover it.
"""

from __future__ import annotations

from pathlib import Path

# The launchers of a version directory, with the gupkg command each one runs.
VERSION_LAUNCHERS = {"install.cmd": "install", "gupkg-tui.cmd": "tui"}

# The one launcher of a ``_check_updates`` folder: it only checks, and never installs.
TRACKER_LAUNCHER = "check-updates.cmd"

# Every launcher name and its command.
LAUNCHERS = {**VERSION_LAUNCHERS, TRACKER_LAUNCHER: "check-updates"}

_TITLES = {
    "install": "Installs this package with gupkg.",
    "tui": "Opens the gupkg text interface (check-updates, update, repair, uninstall, logs) for this package.",
    "check-updates": "Checks for a newer version of this package with gupkg; it installs nothing.",
}


def launcher_text(command: str) -> str:
    """Return the text of one launcher.

    Parameters
    ----------
    command : str
        ``"install"`` for ``install.cmd``, ``"tui"`` for ``gupkg-tui.cmd`` or ``"check-updates"`` for ``check-updates.cmd``.

    Returns
    -------
    str
        The launcher with Windows line endings.

    Raises
    ------
    ValueError
        If *command* is not a launcher command.
    """
    if command not in _TITLES:
        raise ValueError(f"unknown launcher command: {command}")
    template = (Path(__file__).with_name("package-launcher.cmd")).read_text(encoding="ascii")

    # The command line differs per launcher; a tracker launcher names the package by its folder's parent.
    prelude = ""
    line = f'{command} "%GUPKG_HERE%."'
    if command == "check-updates":
        prelude = 'for %%I in ("%GUPKG_PACKAGE_DIR%") do set "GUPKG_NAME=%%~nxI"'
        line = 'check-updates "%GUPKG_NAME%"'
    # install.cmd and check-updates.cmd are started by double-clicking, so they always wait for a key (success or failure) and the
    # window stays readable; gupkg-tui.cmd is a program that was just closed, so it waits only after a failure. GUPKG_NO_PAUSE
    # (set by scripts such as the bootstrap) turns the pause off.
    finish = (
        'if not defined GUPKG_NO_PAUSE pause' if command in ("install", "check-updates")
        else 'if not "%GUPKG_EXIT%"=="0" if not defined GUPKG_NO_PAUSE pause'
    )
    text = template.replace("@@FINISH@@", finish).replace("@@TITLE@@", _TITLES[command]).replace("@@PRELUDE@@", prelude).replace("@@COMMAND@@", f'{line}%GUPKG_EXTRA%')
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def write_launchers(directory: Path, names: tuple[str, ...] | None = None) -> list[Path]:
    """Write launchers into *directory*, replacing existing ones.

    Parameters
    ----------
    directory : Path
        The version directory (or the ``_check_updates`` folder).
    names : tuple of str, optional
        File names to write; ``install.cmd`` and ``gupkg-tui.cmd`` by default.

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
