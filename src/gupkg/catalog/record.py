"""Read and write the install record ``<package>\\.gupkg\\package.toml``.

The install record holds a package's mode, its link to a registry entry (or the opt-out
written by ``gupkg link --unlink``, decision D2) and the integration effects last applied.
Reading never changes anything and never follows a link: a ``.gupkg`` folder or a
``package.toml`` that is a link is reported as a problem and not read, so an elevated run
cannot be led out of a package root by a record. Writing is the one mutation of this
package; the engine (issue 028) calls it, and the catalog's ``describe``, ``list`` and
``resolve`` never do.

Usage and API
-------------
``read_install_record(package_path)`` returns a ``RecordRead`` with the record or the
reason it could not be used. ``write_install_record(package_path, record)`` replaces the
file atomically.

Implementation Approach
-----------------------
The text-to-data conversion lives in ``gupkg.model.state`` (pure); this module adds the
file access and the link checks, and writes through the single TOML writer of
``gupkg.tomlio``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..model.state import InstallRecord, RecordError, install_record_to_data, parse_install_record
from ..tomlio import read_toml, write_toml
from .local import DATA_DIR_NAME, is_reparse_point

RECORD_FILE_NAME = "package.toml"


@dataclass(frozen=True)
class RecordRead:
    """The outcome of reading an install record: the record, or why it cannot be used."""

    record: InstallRecord | None = None
    problem: str | None = None


def record_path(package_path: Path) -> Path:
    """Return where the install record of *package_path* lives."""
    return Path(package_path) / DATA_DIR_NAME / RECORD_FILE_NAME


def read_install_record(package_path: Path) -> RecordRead:
    """Read the install record of a package.

    Parameters
    ----------
    package_path : Path
        The package root.

    Returns
    -------
    RecordRead
        ``record`` is ``None`` and ``problem`` is ``None`` when the package has no record;
        ``problem`` names the reason when the record is a link, unreadable or invalid.
    """
    path = record_path(package_path)
    for entry in (path.parent, path):
        if is_reparse_point(entry):
            return RecordRead(problem=f"{entry} is a link and is not followed")
    if not path.is_file():
        return RecordRead()
    try:
        return RecordRead(record=parse_install_record(read_toml(path)))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, RecordError) as exc:
        return RecordRead(problem=f"Cannot use the install record {path}: {exc}")


def write_install_record(package_path: Path, record: InstallRecord) -> Path:
    """Replace the install record of a package atomically.

    Parameters
    ----------
    package_path : Path
        The package root; its ``.gupkg`` folder is created when needed.
    record : InstallRecord
        The record to write.

    Returns
    -------
    Path
        The file written.

    Raises
    ------
    ValueError
        If ``.gupkg`` or ``package.toml`` is a link, so nothing is written through it.
    OSError
        If the file cannot be written.
    """
    path = record_path(package_path)
    for entry in (path.parent, path):
        if is_reparse_point(entry):
            raise ValueError(f"{entry} is a link; refusing to write the install record through it")
    write_toml(path, install_record_to_data(record))
    return path
