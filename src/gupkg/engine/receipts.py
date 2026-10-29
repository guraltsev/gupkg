"""Write and read receipts: what one install of one version did (design sections 4 and 9.2).

A receipt is the file ``App.info`` in the version directory (``<package>\\<version>\\App.info``, beside ``App`` and ``pkg.toml``), so
a version carries the record of how its ``App`` was built wherever it is copied. The record tells where the version came from (the
``[source]`` kind, the URL, whether a distribution folder it was copied from carried the Mark of the Web),
the checksum facts (decision, observed SHA-256, final URL, size), the fingerprint of the populated ``App`` (the SHA-256 of
every file), the log file of the run and the times. There is one receipt per version, so no code picks one
by modification time. Receipts are data for ``check``, ``info`` and ``gupkg pkg pin``; they are never trusted
as proof, because in user mode the user can edit them.

Usage and API
-------------
``update_receipt(package_path, version, sections)`` merges new facts into the version's receipt (creating it) and
returns the file. ``read_receipt(package_path, version)`` returns a ``ReceiptRead``. ``receipt_checksum(data)`` reads the
``[checksum]`` table strictly. ``list_receipts(package_path)`` names the versions that have a receipt.

Implementation Approach
-----------------------
Writing goes through the one TOML writer, atomically, and refuses a version folder or file
that is a link, so a receipt cannot be written through a link into another place. A later write replaces whole
sections and keeps the others, so the populate step and the activate step each add their own facts.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..catalog.local import is_reparse_point
from ..model.checksum import ChecksumInvalid, ChecksumRecord, parse_checksum_record
from ..tomlio import read_toml, write_toml

RECEIPT_SCHEMA = 1

# The largest receipt that is read. A receipt of a user-mode install is user-writable, so it is untrusted input: 1 MiB
# holds a recorded file list of several thousand files (about 130 bytes each) and keeps ``check`` and ``info`` from
# reading an enormous file. A larger one is reported as a problem, never parsed; an App of more files than that records
# its fingerprint but not the list (see ``MAX_RECORDED_FILES`` in ``gupkg.engine.context``).
MAX_RECEIPT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ReceiptRead:
    """The outcome of reading a receipt: its data, or why there is none (``problem`` is ``None`` for a missing file)."""

    data: Mapping[str, Any] | None = None
    problem: str | None = None


INFO_NAME = "App.info"


def receipt_path(package_path: Path, version: str) -> Path:
    """Return the receipt of one version: the file ``App.info`` in the version directory, beside ``App`` and ``pkg.toml``."""
    return Path(package_path) / version / INFO_NAME


def _check_links(package_path: Path, path: Path) -> None:
    """Refuse to write through a link: the version directory and ``App.info`` itself must not be links."""
    for entry in (path.parent, path):
        if is_reparse_point(entry):
            raise ValueError(f"{entry} is a link; refusing to write the record of the App through it")


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
    for entry in (path.parent, path):
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
    """Return the versions that have a record (an ``App.info`` in the version directory); nothing is read."""
    found: set[str] = set()
    root = Path(package_path)
    try:
        for entry in root.iterdir():
            if entry.is_dir() and not entry.name.startswith((".", "_")) and entry.name.casefold() != "current":
                if (entry / INFO_NAME).is_file() and not is_reparse_point(entry):
                    found.add(entry.name)
    except OSError:
        pass
    return sorted(found)
