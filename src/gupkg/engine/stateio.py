"""Read and write the small machine-state files of the engine: the ad-hoc roots and the last tracker check.

``state.toml`` of a root data directory (``<root>\\.gupkg\\state.toml``) lists the ad-hoc roots: package roots that
were installed in place, outside every configured root, so ``list``, ``check`` and the manager still see them
(design 5.4, 11). Each entry is ``{path, mode}``. ``<package>\\.gupkg\\state\\track.toml`` remembers the result of the
last ``check-updates`` of one package (design 4, 13.1).

Usage and API
-------------
``register_adhoc(settings, env, package_path, mode)`` and ``unregister_adhoc(...)`` edit the ad-hoc list of the
configured root of *mode*; ``write_track_state(package_path, data)`` records a check; ``read_track_state(package_path)``
reads it back.

Implementation Approach
-----------------------
Only the ``adhoc`` key of ``state.toml`` is changed; every other key stays as another reader wrote it. Writing is atomic
and refuses a data folder or file that is a link. The list is de-duplicated by canonical path, so registering a package twice
is harmless.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..catalog.local import DATA_DIR_NAME, canonical_path, is_reparse_point
from ..model.settings import Settings, expand_env
from ..tomlio import read_toml, write_toml


def _state_path(settings: Settings, env: Mapping[str, str], mode: str) -> Path:
    """Return the ``state.toml`` of the configured root of *mode*."""
    return Path(expand_env(settings[f"roots.{mode}"], env)) / DATA_DIR_NAME / "state.toml"


def _read(path: Path) -> dict[str, Any]:
    """Read *path* as TOML data; a missing file is empty, an unreadable one is an error (it is never silently replaced)."""
    if not path.is_file():
        return {}
    try:
        return read_toml(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Cannot read {path}: {error}") from error


def _write(path: Path, data: dict[str, Any]) -> None:
    """Write *data* atomically, refusing a link."""
    for entry in (path.parent, path):
        if is_reparse_point(entry):
            raise ValueError(f"{entry} is a link; refusing to write through it")
    write_toml(path, data)


def register_adhoc(settings: Settings, env: Mapping[str, str], package_path: Path, mode: str) -> None:
    """Record *package_path* as an ad-hoc package root of *mode* in the configured root's ``state.toml``.

    Raises
    ------
    ValueError
        If ``state.toml`` is a link or cannot be read.
    """
    path = _state_path(settings, env, mode)
    data = _read(path)
    entries = [item for item in data.get("adhoc", []) if isinstance(item, dict)]
    key = canonical_path(package_path)
    if any(canonical_path(str(item.get("path", ""))) == key for item in entries):
        return
    data["adhoc"] = [*entries, {"path": str(package_path), "mode": mode}]
    _write(path, data)


def unregister_adhoc(settings: Settings, env: Mapping[str, str], package_path: Path) -> None:
    """Remove *package_path* from the ad-hoc list of every configured root's ``state.toml``."""
    key = canonical_path(package_path)
    for mode in ("system", "user", "portable"):
        path = _state_path(settings, env, mode)
        if not path.is_file():
            continue
        data = _read(path)
        entries = [item for item in data.get("adhoc", []) if isinstance(item, dict)]
        kept = [item for item in entries if canonical_path(str(item.get("path", ""))) != key]
        if len(kept) != len(entries):
            data["adhoc"] = kept
            if not kept:
                del data["adhoc"]
            _write(path, data)


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
