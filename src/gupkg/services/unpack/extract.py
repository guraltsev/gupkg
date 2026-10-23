"""Extract one archive into an empty folder, refusing anything unsafe before and after.

Zip archives are read in process so that every member name, type, size and byte count is
judged before and while it is written. Every other format goes through 7-Zip: the table of
contents is read and judged first (``gupkg.services.unpack.sevenzip``), 7-Zip unpacks into
the folder, and the finished tree is walked again for links and size. Compressed tarballs
take two 7-Zip passes: decompress to a tar, then unpack the tar (decision D39).

Usage and API
-------------
``extract_archive(system, archive, kind, destination, limits)`` fills the empty folder
*destination* and returns nothing; it raises ``UnpackError`` for an unsafe, oversized,
encrypted or damaged archive. ``Limits`` bounds entries and sizes. ``check_tree`` is the
post-extraction walk, also used after transformations.

Implementation Approach
-----------------------
Names are validated as a set (``check_member_set``), so duplicates and names that collide
on a case-insensitive disk are refused. Sizes are bounded twice: by the declared sizes
before extraction and by the bytes actually written (zip) or found on disk afterwards
(7-Zip). The caller owns *destination* and deletes it on any failure, so no partial result
survives.
"""

from __future__ import annotations

import lzma
import os
import shutil
import stat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from ...system.port import System
from ..safepath import UnsafePath, check_member_set, confine, is_reparse_point, safe_file_name
from .sevenzip import Listing, SevenZip, SevenZipError

_COPY_BLOCK = 1024 * 1024
_NOT_REGULAR = (0o020000, 0o060000, 0o010000, 0o140000)  # device, block, fifo, socket


class UnpackError(RuntimeError):
    """Report an archive that is unsafe, too large, encrypted, damaged or not what was declared."""


@dataclass(frozen=True)
class Limits:
    """Bound what one unpack may produce, against archive bombs.

    Attributes
    ----------
    max_entries : int
        Most members (files and folders) an archive may hold.
    max_total_bytes : int
        Most uncompressed bytes in total.
    max_file_bytes : int
        Most uncompressed bytes in one file.
    """

    max_entries: int = 1_000_000
    max_total_bytes: int = 8 * 1024**3
    max_file_bytes: int = 4 * 1024**3


def extract_archive(system: System, archive: Path, kind: str, destination: Path, limits: Limits,
                    seven_zip: Path | None = None) -> None:
    """Extract *archive* of the given *kind* into the empty folder *destination*.

    Parameters
    ----------
    system : System
        The boundary used to run 7-Zip.
    archive : Path
        The archive file.
    kind : str
        ``zip``, ``7z``, ``tar``, ``sfx``, ``nsis`` or ``msi``.
    destination : Path
        An existing, empty folder that receives the contents.
    limits : Limits
        The entry and size bounds.
    seven_zip : Path, optional
        The 7-Zip executable for every kind but ``zip``.

    Raises
    ------
    UnpackError
        If anything about the archive is unsafe, over a limit, or fails to extract.
    """
    try:
        if kind == "zip":
            _extract_zip(archive, destination, limits)
        else:
            tool = SevenZip(system, seven_zip)
            if kind == "tar":
                _extract_tar(tool, archive, destination, limits)
            else:
                _extract_with_seven_zip(tool, archive, destination, limits, kind)
    except (UnsafePath, SevenZipError, zipfile.BadZipFile, NotImplementedError, EOFError, zlib.error, lzma.LZMAError, OSError) as error:
        raise UnpackError(str(error)) from error
    check_tree(destination, limits)


def check_tree(root: Path, limits: Limits) -> tuple[int, int]:
    """Walk *root* without following links; return ``(file count, byte count)``.

    Raises
    ------
    UnpackError
        If any entry is a link, junction or special file, or a limit is exceeded.
    """
    files = total = entries = 0
    for current, folders, names in os.walk(root, followlinks=False):
        for name in [*folders, *names]:
            entries += 1
            path = Path(current) / name
            if is_reparse_point(path):
                raise UnpackError(f"The archive produced a link or junction at {path.relative_to(root).as_posix()}")
            if entries > limits.max_entries:
                raise UnpackError(f"The archive has more than {limits.max_entries} entries")
        for name in names:
            path = Path(current) / name
            status = os.lstat(path)
            if not stat.S_ISREG(status.st_mode):
                raise UnpackError(f"The archive produced a special file at {path.relative_to(root).as_posix()}")
            files += 1
            total += status.st_size
            if status.st_size > limits.max_file_bytes or total > limits.max_total_bytes:
                raise UnpackError("The archive is larger than the allowed size when unpacked")
    return files, total


# ---------------------------------------------------------------------------
# zip, in process
# ---------------------------------------------------------------------------


def _extract_zip(archive: Path, destination: Path, limits: Limits) -> None:
    """Write every member of a zip archive below *destination*, judging each one first."""
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if len(infos) > limits.max_entries:
            raise UnpackError(f"The archive has {len(infos)} entries; at most {limits.max_entries} are allowed")
        names = check_member_set(info.filename for info in infos)
        declared = sum(info.file_size for info in infos)
        if declared > limits.max_total_bytes or any(info.file_size > limits.max_file_bytes for info in infos):
            raise UnpackError("The archive declares more uncompressed data than the allowed size")

        # Refuse members that are not plain files or folders, and encrypted members.
        for info in infos:
            kind = (info.external_attr >> 16) & 0o170000
            if kind == 0o120000:
                raise UnpackError(f"The archive contains a symbolic link: {info.filename!r}")
            if kind in _NOT_REGULAR:
                raise UnpackError(f"The archive contains a special file: {info.filename!r}")
            if info.flag_bits & 0x1:
                raise UnpackError("The archive is encrypted")

        written = 0
        for info, name in zip(infos, names):
            target = confine(destination, name)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)

            # Count the bytes really produced, not the header's claim, and create the
            # file exclusively so a repeated name cannot overwrite an earlier member.
            produced = 0
            with bundle.open(info) as source, open(target, "xb") as sink:
                while block := source.read(_COPY_BLOCK):
                    produced += len(block)
                    written += len(block)
                    if produced > info.file_size or produced > limits.max_file_bytes or written > limits.max_total_bytes:
                        raise UnpackError(f"{info.filename!r} expands past its declared or the allowed size")
                    sink.write(block)


# ---------------------------------------------------------------------------
# 7-Zip based kinds
# ---------------------------------------------------------------------------


def _judge(listing: Listing, limits: Limits, *, allow_empty: bool = True) -> None:
    """Refuse a table of contents with an unsafe name, a link, encryption, or a size over a limit."""
    entries = listing.entries
    if len(entries) > limits.max_entries:
        raise UnpackError(f"The archive has {len(entries)} entries; at most {limits.max_entries} are allowed")
    check_member_set(entry.path for entry in entries)
    for entry in entries:
        if entry.encrypted:
            raise UnpackError("The archive is encrypted")
        if entry.is_link:
            raise UnpackError(f"The archive contains a link: {entry.path!r}")
        if entry.size > limits.max_file_bytes:
            raise UnpackError(f"{entry.path!r} is larger than the allowed size")
    if sum(entry.size for entry in entries) > limits.max_total_bytes:
        raise UnpackError("The archive declares more uncompressed data than the allowed size")
    if not entries and not allow_empty:
        raise UnpackError("The archive is empty")


def _extract_with_seven_zip(tool: SevenZip, archive: Path, destination: Path, limits: Limits, kind: str) -> None:
    """Extract a 7z, self-extracting, NSIS or MSI file after judging its table of contents."""
    forced = "Nsis" if kind == "nsis" else None
    listing = tool.listing(archive, forced)
    if kind == "7z" and listing.type != "7z":
        raise UnpackError(f"{archive.name} is not a 7z archive (7-Zip reads it as {listing.type})")
    if kind == "nsis" and listing.type.lower() != "nsis":
        raise UnpackError(f"{archive.name} is not an NSIS installer (7-Zip reads it as {listing.type})")
    if kind == "sfx" and listing.type == "PE":
        raise UnpackError(f"{archive.name} is an executable with no archive inside")
    _judge(listing, limits)
    tool.extract(archive, destination, forced)


def _extract_tar(tool: SevenZip, archive: Path, destination: Path, limits: Limits) -> None:
    """Unpack a tarball with two 7-Zip passes: decompress, then unpack the tar."""
    first = tool.listing(archive)

    # A plain tar needs only the second pass; any compressed wrapper is removed first.
    if first.type.lower() == "tar":
        _judge(first, limits)
        tool.extract(archive, destination)
        return
    wrapper = destination.parent / f".{destination.name}-pass1"
    wrapper.mkdir()
    try:
        _judge(first, limits)
        if len(first.entries) > 1:
            raise UnpackError("A compressed tarball wraps exactly one file")
        tool.extract(archive, wrapper)
        produced = [path for path in wrapper.iterdir()]
        if len(produced) != 1 or not produced[0].is_file() or is_reparse_point(produced[0]):
            raise UnpackError("Decompressing the tarball did not produce exactly one file")
        inner = produced[0]
        second = tool.listing(inner, "tar") if _looks_like_tar(inner) else None
        if second is None:
            # Not a tarball after all: the decompressed file is the content.
            shutil.move(str(inner), str(destination / safe_file_name(inner.name)))
            return
        _judge(second, limits)
        tool.extract(inner, destination, "tar")
    finally:
        shutil.rmtree(wrapper, ignore_errors=True)


def _looks_like_tar(path: Path) -> bool:
    """Return whether a file has the ``ustar`` marker (or a tar name) of a tar archive."""
    with open(path, "rb") as handle:
        head = handle.read(512)
    return head[257:262] == b"ustar" or path.suffix.lower() == ".tar"
