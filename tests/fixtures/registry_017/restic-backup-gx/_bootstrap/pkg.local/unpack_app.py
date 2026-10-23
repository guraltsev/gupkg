"""Place the restic-backup-gx executable into the staged application tree.

The release is one executable whose published name contains the version. This
module copies it to a fixed name so shortcuts do not change between versions.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The downloaded artifact is copied to ``restic-backup-gx.exe`` inside the
manager-owned staging directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Copy the downloaded executable into staged ``App`` under a stable name.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.
    """
    paths = context["paths"]
    stage_app = Path(paths["stageApp"])
    stage_app.mkdir(parents=True)
    shutil.copy2(Path(paths["artifact"]), stage_app / "restic-backup-gx.exe")
