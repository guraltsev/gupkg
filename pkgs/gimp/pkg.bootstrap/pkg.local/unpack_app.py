"""Install the GIMP Inno Setup package into the staged application tree.

The official GIMP download is an Inno Setup installer, a format 7-Zip cannot
unpack. This module runs the installer silently with its target directory set
to the staged ``App`` folder, so the program files land in the version tree.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The installer runs per-user (no elevation) with start-menu and desktop icons
disabled. Inno Setup may still record an uninstall entry for the current user
under ``HKCU``; that entry is not managed by the package manager.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Silently install the downloaded GIMP setup into staged ``App``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])

    command = [
        str(artifact),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/CURRENTUSER",
        "/NOICONS",
        f"/DIR={stage_app}",
    ]
    subprocess.run(command, check=True)
    if not (stage_app / "bin" / "gimp-3.exe").is_file():
        raise RuntimeError("The GIMP installer did not produce bin/gimp-3.exe")
