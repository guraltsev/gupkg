"""Write and read receipts: what one install of one version did (design sections 4 and 9.2).

A receipt ``<package>\\.gupkg\\receipts\\<version>.toml`` records where the version came from (the
``[source]`` kind, the URL, whether a distribution folder it was copied from carried the Mark of the Web),
the checksum facts (decision, observed SHA-256, final URL, size), the fingerprint of the populated ``App`` (the SHA-256 of
every file), the log file of the run and the times. There is one receipt per version, so no code picks one
by modification time. Receipts are data for ``check``, ``info`` and ``gupkg pkg pin``; they are never trusted
as proof, because in user and portable mode the user can edit them.

Usage and API
-------------
``update_receipt(package_path, version, sections)`` merges new facts into the version's receipt (creating it) and
returns the file. ``read_receipt(package_path, version)`` returns a ``ReceiptRead``. ``receipt_checksum(data)`` reads the
``[checksum]`` table strictly. ``list_receipts(package_path)`` names the versions that have a receipt.

Implementation Approach
-----------------------
Writing goes through the one TOML writer, atomically, and refuses a ``.gupkg`` folder, ``receipts`` folder or file
that is a link, so a receipt cannot be written through a link into another place. A later write replaces whole
sections and keeps the others, so the populate step and the activate step each add their own facts.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..catalog.local import DATA_DIR_NAME, is_reparse_point
from ..model.checksum import ChecksumInvalid, ChecksumRecord, parse_checksum_record
from ..tomlio import read_toml, write_toml

RECEIPT_SCHEMA = 1

# The largest receipt that is read (200,000 files at about 130 bytes each fit with room to spare).
MAX_RECEIPT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ReceiptRead:
    """The outcome of reading a receipt: its data, or why there is none (``problem`` is ``None`` for a missing file)."""

    data: Mapping[str, Any] | None = None
    problem: str | None = None


def receipts_dir(package_path: Path) -> Path:
    """Return the folder that holds the receipts of a package."""
    return Path(package_path) / DATA_DIR_NAME / "receipts"


def receipt_path(package_path: Path, version: str) -> Path:
    """Return the receipt file of one version."""
    return receipts_dir(package_path) / f"{version}.toml"


def _check_links(package_path: Path, path: Path) -> None:
    """Refuse to write through a link: ``.gupkg``, ``receipts`` and the file itself must not be links."""
    for entry in (Path(package_path) / DATA_DIR_NAME, path.parent, path):
        if is_reparse_point(entry):
            raise ValueError(f"{entry} is a link; refusing to write a receipt through it")


def update_receipt(package_path: Path, version: str, sections: Mapping[str, Any]) -> Path:
    """Merge *sections* into the receipt of *version*, creating the receipt when it does not exist.

    Parameters
    ----------
    package_path : Path
        The package root.
    version : str
        The version the receipt describes.
    sections : mapping
        Keys to set. A key replaces the receipt's value of that key whole (so a ``[checksum]`` table is
        always one install's complete record); other keys stay.

    Returns
    -------
    Path
        The receipt file.

    Raises
    ------
    ValueError
        If ``.gupkg``, ``receipts`` or the file is a link.
    OSError
        If the file cannot be written.
    """
    path = receipt_path(package_path, version)
    _check_links(package_path, path)
    existing = read_receipt(package_path, version).data or {}
    merged = {"schema": RECEIPT_SCHEMA, **dict(existing), **dict(sections)}
    write_toml(path, merged)
    return path


def read_receipt(package_path: Path, version: str) -> ReceiptRead:
    """Read the receipt of *version*; a missing file is no problem, an unreadable or linked one is."""
    path = receipt_path(package_path, version)
    for entry in (Path(package_path) / DATA_DIR_NAME, path.parent, path):
        if is_reparse_point(entry):
            return ReceiptRead(problem=f"{entry} is a link and is not followed")
    if not path.is_file():
        return ReceiptRead()

    # A receipt of a user-mode install is user-writable: an absurdly large one is refused unread.
    try:
        size = path.stat().st_size
    except OSError as exc:
        return ReceiptRead(problem=f"Cannot read the receipt {path}: {exc}")
    if size > MAX_RECEIPT_BYTES:
        return ReceiptRead(problem=f"The receipt {path} is {size} bytes; one over {MAX_RECEIPT_BYTES} is not read")
    try:
        data = read_toml(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return ReceiptRead(problem=f"Cannot read the receipt {path}: {exc}")
    if data.get("schema") != RECEIPT_SCHEMA:
        return ReceiptRead(problem=f"The receipt {path} has an unsupported schema ({data.get('schema')!r})")
    return ReceiptRead(data)


def receipt_checksum(data: Mapping[str, Any]) -> ChecksumRecord | None:
    """Return the ``[checksum]`` record of a receipt, or ``None`` when it has none or it is not valid."""
    table = data.get("checksum")
    if table is None:
        return None
    try:
        return parse_checksum_record(table)
    except ChecksumInvalid:
        return None


def list_receipts(package_path: Path) -> list[str]:
    """Return the versions that have a receipt file (names only; nothing is read)."""
    folder = receipts_dir(package_path)
    if is_reparse_point(folder) or not folder.is_dir():
        return []
    return sorted(item.stem for item in folder.iterdir() if item.suffix == ".toml" and item.is_file())
