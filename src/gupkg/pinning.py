"""Copy a receipt's observed hash into a version's ``pkg.toml`` (``gupkg pkg pin``, design 9.2).

gupkg never writes an observed hash into ``pkg.toml`` by itself. Pinning is the explicit,
reviewable act that does it: it takes the hash a receipt recorded for a download that was
installed without a checksum, and sets ``sha256`` and ``sha256_from = "manual"`` in the
``[source]`` table of the version, so the next install verifies against it.

Usage and API
-------------
``pin_hash(version_dir, record)`` edits ``<version_dir>\\pkg.toml`` and returns a ``PinResult``;
``pin_text(text, record)`` is the same edit on text. ``record`` is the receipt's
``ChecksumRecord`` (``gupkg.model.checksum.parse_checksum_record``). Errors are ``PinError``.

Implementation Approach
-----------------------
The edit is made on the text so comments and layout survive, and it is *proved*: the new text
must parse as a version manifest, and its data must equal the old data with only ``sha256``
and ``sha256_from`` changed. If the file is written in a shape the line edit cannot handle
(an inline table, a dotted key), nothing is written and the error says to edit it by hand.
Only a record of a download with an observed hash, for the same URL as the manifest, is pinned.
A manifest that already carries a different hash is a conflict, never overwritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .core import write_text_atomic
from .model.checksum import DOWNLOAD_DECISIONS, ChecksumRecord, normalize_sha256
from .model.manifest import ManifestError, ManifestKind, manifest_to_data, parse_manifest

_SOURCE_HEADER = re.compile(r"^[ \t]*\[source\][ \t]*(#.*)?$")
_ANY_HEADER = re.compile(r"^[ \t]*\[")
_KEY = {"sha256": re.compile(r"^[ \t]*sha256[ \t]*=", re.IGNORECASE), "sha256_from": re.compile(r"^[ \t]*sha256_from[ \t]*=")}


class PinError(ValueError):
    """Report that a hash cannot be pinned, and why."""


@dataclass(frozen=True)
class PinResult:
    """What a pin did: the new text and whether it differs from the old."""

    text: str
    changed: bool


def pin_text(text: str, record: ChecksumRecord) -> PinResult:
    """Return *text* (a version's ``pkg.toml``) with the hash of *record* pinned.

    Raises
    ------
    PinError
        If the record has no observed hash, is not a download's, names another URL, the manifest
        has no download, uses a git source, already has a different hash, or the file cannot be
        edited safely.
    """
    if record.decision not in DOWNLOAD_DECISIONS or record.observed_sha256 is None:
        raise PinError("the receipt has no observed SHA-256 of a download, so there is nothing to pin")
    observed = normalize_sha256(record.observed_sha256)
    try:
        before = parse_manifest(text, ManifestKind.VERSION)
    except ManifestError as error:
        raise PinError(f"pkg.toml is not a valid version manifest: {error}") from error
    source = before.source
    if source.download_url is None:
        raise PinError("this version has no download_url, so there is no download to pin a hash for")
    if source.kind == "git":
        raise PinError("a git source is pinned by its commit ([source.git] commit), not by a SHA-256")
    if record.url != source.download_url:
        raise PinError(
            f"the receipt is for {record.url}, but pkg.toml downloads {source.download_url}; refusing to pin a hash for another address"
        )
    if source.sha256 is not None:
        if source.sha256 == observed:
            return PinResult(text, False)
        raise PinError(
            f"pkg.toml already declares SHA-256 {source.sha256}, which differs from the observed {observed}; "
            "resolve that by hand"
        )

    new_text = _insert(text, observed)
    try:
        after = parse_manifest(new_text, ManifestKind.VERSION)
    except ManifestError as error:
        raise PinError(f"pkg.toml has a shape that cannot be edited safely ({error}); add sha256 = \"{observed}\" by hand") from error
    expected = manifest_to_data(before)
    expected.setdefault("source", {}).update({"sha256": observed, "sha256_from": "manual"})
    if manifest_to_data(after) != expected:
        raise PinError(f"pkg.toml has a shape that cannot be edited safely; add sha256 = \"{observed}\" by hand")
    return PinResult(new_text, True)


def _insert(text: str, observed: str) -> str:
    """Insert the two keys after the ``[source]`` header, replacing a key that is already there."""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    start = next((index for index, line in enumerate(lines) if _SOURCE_HEADER.match(line.rstrip("\r\n"))), None)
    if start is None:
        raise PinError("pkg.toml has no [source] table of its own; add sha256 by hand")
    end = next((index for index in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[index])), len(lines))
    kept = [line for line in lines[start + 1:end] if not any(pattern.match(line) for pattern in _KEY.values())]
    added = [f'sha256 = "{observed}"{newline}', f'sha256_from = "manual"{newline}']
    return "".join(lines[:start + 1] + added + kept + lines[end:])


def pin_hash(version_dir: Path, record: ChecksumRecord) -> PinResult:
    """Pin *record*'s hash into ``<version_dir>\\pkg.toml`` and write the file atomically.

    Raises
    ------
    PinError
        As ``pin_text``, or if the file cannot be read or written.
    """
    path = Path(version_dir) / "pkg.toml"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise PinError(f"cannot read {path}: {error}") from error
    result = pin_text(text, record)
    if result.changed:
        try:
            write_text_atomic(path, result.text)
        except OSError as error:
            raise PinError(f"cannot write {path}: {error}") from error
    return result
