"""Unpack the official Firefox installer into the staged application tree.

The Firefox installer is a 7-Zip self-extracting archive holding the whole
browser in a ``core`` folder next to ``setup.exe``. This module extracts it
with the package manager's bundled 7-Zip and keeps only the ``core`` contents,
without executing the installer or touching system installation state.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The archive is extracted into a scratch folder beside ``App`` inside the
manager-owned staging directory, then ``core`` is renamed to ``App``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from gupkg.extractors import find_7z


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the downloaded Firefox installer into staged ``App``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.

    Raises
    ------
    RuntimeError
        The installer does not contain a ``core`` folder with ``firefox.exe``.
    subprocess.CalledProcessError
        7-Zip fails to extract the installer.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])

    # Extract beside App so a failure leaves nothing behind in the final tree.
    scratch = stage_app.with_name(stage_app.name + ".extract")
    scratch.mkdir(parents=True)
    try:
        command = [str(find_7z()), "x", "-y", f"-o{scratch}", str(artifact), "core"]
        subprocess.run(command, check=True)
        core = scratch / "core"
        if not (core / "firefox.exe").is_file():
            raise RuntimeError("Firefox installer does not contain core/firefox.exe")
        core.rename(stage_app)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
