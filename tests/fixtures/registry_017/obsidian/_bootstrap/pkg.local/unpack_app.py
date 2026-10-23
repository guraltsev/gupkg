"""Extract the application bundled inside an NSIS installer.

The release is an NSIS installer that carries the program as an inner 7-Zip
archive. This module opens the installer and the inner archive with the package
manager's bundled 7-Zip extractor, so no installer code runs and the system is
not modified.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading a checked
candidate. The function creates the staged ``App`` tree used for atomic package
activation.

Implementation Approach
-----------------------
The installer is unpacked into a scratch folder beside ``App``, and the inner
archive is then extracted into ``App``. The scratch folder is removed afterward.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from gupkg.extractors import find_7z


PKG_MODULE_API = 1

_INNER_ARCHIVE = "app-64.7z"


def unpack_app(context: dict[str, Any]) -> None:
    """Extract the installer's bundled application into staged ``App``.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the downloaded artifact and staging paths.

    Raises
    ------
    RuntimeError
        The installer does not contain the expected inner archive.
    """
    paths = context["paths"]
    artifact = Path(paths["artifact"])
    stage_app = Path(paths["stageApp"])
    scratch = Path(paths["stageRoot"]) / "installer"

    # Unpack the installer first; its payload is an inner archive.
    scratch.mkdir(parents=True)
    seven_zip = str(find_7z())
    subprocess.run([seven_zip, "x", "-y", f"-o{scratch}", str(artifact)], check=True)
    inner = scratch / "$PLUGINSDIR" / _INNER_ARCHIVE
    if not inner.is_file():
        raise RuntimeError(f"Installer does not contain {_INNER_ARCHIVE}")

    stage_app.mkdir(parents=True)
    subprocess.run([seven_zip, "x", "-y", f"-o{stage_app}", str(inner)], check=True)
    shutil.rmtree(scratch, ignore_errors=True)
