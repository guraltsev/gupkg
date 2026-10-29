"""Read and write the small machine-state file of the engine: the last tracker check of a package.

``<package>\\.gupkg\\state\\track.toml`` remembers the result of the last ``check-updates`` of one package (design 4, 13.1).
(``<root>\\.gupkg\\state.toml`` keeps only the last registry sync, which ``gupkg.catalog.registry`` writes.)

Usage and API
-------------
``write_track_state(package_path, data)`` records a check; ``read_track_state(package_path)`` reads it back.

Implementation Approach
-----------------------
Writing is atomic and refuses a data folder or file that is a link.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..catalog.local import DATA_DIR_NAME, is_reparse_point
from ..tomlio import read_toml, write_toml


def _write(path: Path, data: dict[str, Any]) -> None:
    """Write *data* atomically, refusing a link."""
    for entry in (path.parent, path):
        if is_reparse_point(entry):
            raise ValueError(f"{entry} is a link; refusing to write through it")
    write_toml(path, data)


def track_state_path(package_path: Path) -> Path:
    """Return the file that remembers the last ``check-updates`` of a package."""
    return Path(package_path) / DATA_DIR_NAME / "state" / "track.toml"


def write_track_state(package_path: Path, data: Mapping[str, Any]) -> Path:
    """Record the result of a ``check-updates`` run (the version found, whether it was populated, when)."""
    path = track_state_path(package_path)
    _write(path, {"schema": 1, **dict(data)})
    return path


def read_track_state(package_path: Path) -> dict[str, Any] | None:
    """Return the recorded last check of a package, or ``None`` when there is none or it cannot be read."""
    path = track_state_path(package_path)
    if not path.is_file() or is_reparse_point(path):
        return None
    try:
        return read_toml(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
