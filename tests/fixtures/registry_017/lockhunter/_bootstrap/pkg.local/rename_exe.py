"""Give the portable download its stable program name.

The publisher ships the portable edition as a single executable whose file
name embeds the version. A stable name keeps shortcuts valid across updates.

Usage and API
-------------
The package manager calls ``install_step(context)`` after the payload has been
unpacked and before the new version is committed.

Implementation Approach
-----------------------
The one executable matching the publisher's name pattern is renamed inside the
staged ``App`` tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


PKG_MODULE_API = 1

_PATTERN = "lockhuntersetup_portable_*.exe"
_STABLE_NAME = "LockHunter.exe"


def install_step(context: dict[str, Any]) -> None:
    """Rename the downloaded executable to its stable name.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the staged paths.

    Raises
    ------
    RuntimeError
        The staged application does not contain exactly one matching file.
    """
    stage_app = Path(context["paths"]["stageApp"])
    matches = list(stage_app.glob(_PATTERN))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {_PATTERN} in the staged application")
    matches[0].rename(stage_app / _STABLE_NAME)
