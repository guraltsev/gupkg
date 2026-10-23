"""Unpack the Pidgin offline installer into the staged application tree.

The offline installer is an NSIS archive. It holds the Pidgin files directly
plus ``gtk.zip``, the GTK runtime the installer would place in ``Gtk``. This
module reproduces that layout in ``App`` without running the installer or
modifying system installation state.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The bundled 7-Zip extracts the NSIS payload, ``gtk.zip`` is unpacked into
``App``, and installer-only files (the plug-in folder, debug symbols, and the
uninstaller) are removed.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from gupkg.extractors import find_7z


PKG_MODULE_API = 1

_INSTALLER_ONLY = ("$PLUGINSDIR", "pidgin-uninst.exe")


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the downloaded Pidgin installer into staged ``App``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.

    Raises
    ------
    RuntimeError
        The installer lacks ``pidgin.exe`` or the GTK runtime archive.
    subprocess.CalledProcessError
        7-Zip fails to extract the installer.
    zipfile.BadZipFile
        The bundled GTK archive is not a valid ZIP file.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])

    stage_app.mkdir(parents=True)
    command = [str(find_7z()), "x", "-y", "-tNsis", f"-o{stage_app}", str(artifact)]
    subprocess.run(command, check=True)

    gtk_archive = stage_app / "$PLUGINSDIR" / "gtk.zip"
    if not (stage_app / "pidgin.exe").is_file() or not gtk_archive.is_file():
        raise RuntimeError("Pidgin installer lacks pidgin.exe or the GTK runtime")

    # The GTK archive already contains a top-level Gtk folder.
    with zipfile.ZipFile(gtk_archive) as archive:
        for name in archive.namelist():
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                raise RuntimeError(f"GTK archive contains unsafe path: {name}")
        archive.extractall(stage_app)

    for name in _INSTALLER_ONLY:
        target = stage_app / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
