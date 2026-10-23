"""Extract a downloaded 7-Zip archive into the staged application tree.

The release is a ``.7z`` archive, which the ZIP payload cannot read. This
module uses the package manager's bundled 7-Zip extractor to populate ``App``
without running an installer or changing system state.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The archive is extracted into the manager-owned staging directory. When the
archive wraps everything in one top-level folder, that folder's contents are
moved up so ``App`` holds the program files directly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from gupkg.extractors import find_7z


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the downloaded archive into staged ``App``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])

    # Extract inside the staged tree so a failure leaves the live version
    # untouched.
    stage_app.mkdir(parents=True)
    command = [str(find_7z()), "x", "-y", f"-o{stage_app}", str(artifact)]
    subprocess.run(command, check=True)

    # Flatten a single wrapper directory so shortcuts can use stable paths.
    entries = list(stage_app.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        wrapper = entries[0]
        for child in list(wrapper.iterdir()):
            shutil.move(str(child), str(stage_app / child.name))
        wrapper.rmdir()
