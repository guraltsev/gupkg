"""Install Volta and create its tool shims in the staged application tree.

Volta's Windows release ZIP contains only ``volta.exe``, ``volta-shim.exe``,
and ``volta-migrate.exe``. ``volta setup`` normally makes the tool shims by
copying ``volta-shim.exe``; this module does that during staging so no
installer or profile modification runs.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a selected
release. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The module extracts the flat release ZIP into the manager-owned staging
directory, then copies ``volta-shim.exe`` once per shimmed tool.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


PKG_MODULE_API = 1

# Tools Volta shims on Windows, per the Volta installer documentation.
_SHIMMED_TOOLS = ("node", "npm", "npx", "pnpm", "yarn")
_REQUIRED_FILES = ("volta.exe", "volta-shim.exe")


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the Volta release ZIP and create tool shims in staged ``App``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.

    Raises
    ------
    RuntimeError
        The archive contains an unsafe path or lacks a required executable.
    zipfile.BadZipFile
        The downloaded artifact is not a valid ZIP file.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])
    stage_app.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(artifact) as archive:
        for member in archive.infolist():
            relative_path = PurePosixPath(member.filename.replace("\\", "/"))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise RuntimeError(f"Volta archive contains unsafe path: {member.filename}")
            if member.is_dir():
                continue
            # Volta's archive is flat, so extract each file directly into App.
            with archive.open(member) as source:
                with (stage_app.joinpath(*relative_path.parts)).open("wb") as target:
                    shutil.copyfileobj(source, target)

    for name in _REQUIRED_FILES:
        if not (stage_app / name).is_file():
            raise RuntimeError(f"Volta archive does not contain {name}")

    # Each shim is a byte-for-byte copy: volta-shim.exe dispatches on the name
    # it was launched as, so copies must stay separate files, not links.
    for tool in _SHIMMED_TOOLS:
        shutil.copyfile(stage_app / "volta-shim.exe", stage_app / f"{tool}.exe")
