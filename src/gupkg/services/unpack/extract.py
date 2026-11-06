"""Extract one archive into an empty folder, refusing anything unsafe before, while and after.

Zip archives are handled in process so that every member name, type, size and byte count is
judged before and while it is written. Gzip, bzip2 and xz wrappers of a tarball are
decompressed in process with a byte cap, so a bomb stops at the cap. Every other format goes
through 7-Zip: the table of contents is listed and judged first (``gupkg.services.unpack.sevenzip``),
7-Zip unpacks into the folder while a watcher measures that folder and kills 7-Zip within one poll
of a limit being crossed, and the finished tree is walked again for links and size. A tarball takes two
passes: decompress to a tar, then unpack the tar (decision D39).

Usage and API
-------------
``extract_archive(system, archive, kind, destination, budget)`` fills the empty folder
*destination* and returns nothing; it raises ``UnpackError`` for an unsafe, oversized,
encrypted or damaged archive. ``Limits`` bounds entries and sizes; one ``Budget`` is shared by
an artifact and every archive nested in it, so the limits are cumulative. ``check_tree`` is the
post-extraction walk, also used after transformations. ``decompress_capped`` is the capped
in-process decompressor.

Implementation Approach
-----------------------
Declared sizes are only a hint (a bzip2 or NSIS listing has none, and a trailer can lie), so
the real bound is measured: bytes written (zip, in-process decompression) or the size and entry
count of the staging folder, polled while 7-Zip runs. The caller owns *destination* and deletes
it on any failure, so no partial result survives.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import stat
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ...system.port import System
from ..safepath import UnsafePath, check_member_set, confine, is_reparse_point, safe_file_name
from .sevenzip import Listing, SevenZip, SevenZipError

# Size of the blocks used when copying and decompressing.
_COPY_BLOCK = 1024 * 1024
# Memory the xz decoder may use, so a hostile dictionary size fails before it is allocated.
XZ_MEMORY_LIMIT = 256 * 1024 * 1024
# Unix file-type bits of members that are not regular files or folders.
_NOT_REGULAR = (0o020000, 0o060000, 0o010000, 0o140000)  # device, block, fifo, socket
# Signature and opener of each compression wrapper that is decompressed in process.
_DECOMPRESSORS = (
    (b"\x1f\x8b", gzip.open),
    (b"BZh", bz2.open),
    (b"\xfd7zXZ\x00", lzma.open),
)


class UnpackError(RuntimeError):
    """Report an archive that is unsafe, too large, encrypted, damaged or not what was declared."""


@dataclass(frozen=True)
class Limits:
    """Bound what one unpack may produce, against archive bombs.

    Attributes
    ----------
    max_entries : int
        Most members (files and folders) in total, nested archives included.
    max_total_bytes : int
        Most uncompressed bytes in total, nested archives included.
    max_file_bytes : int
        Most uncompressed bytes in one file.

    Examples
    --------
    Basic usage:

    >>> limits = Limits(max_entries=10)
    >>> limits.max_entries
    10
    >>> limits.max_file_bytes // 1024 ** 3
    4
    """

    max_entries: int = 200_000
    max_total_bytes: int = 8 * 1024**3
    max_file_bytes: int = 4 * 1024**3


@dataclass
class Budget:
    """Track what one artifact and the archives nested in it have already produced.

    A fresh budget starts empty; each extraction charges what it wrote, and the next one may
    use only what is left, so a nested archive cannot start over with a full allowance.

    Methods
    -------
    charge
        Record *files* entries and *size* bytes as produced.

    Attributes
    ----------
    limits : Limits
        The allowance a whole artifact may use.
    used_bytes : int
        Bytes charged so far.
    used_entries : int
        Entries charged so far.
    bytes_left : int
        Return the bytes the next extraction may still write.
    entries_left : int
        Return the entries the next extraction may still create.
    """

    limits: Limits = field(default_factory=Limits)
    used_bytes: int = 0
    used_entries: int = 0

    @property
    def bytes_left(self) -> int:
        """Return the bytes the next extraction may still write."""
        return max(self.limits.max_total_bytes - self.used_bytes, 0)

    @property
    def entries_left(self) -> int:
        """Return the entries the next extraction may still create."""
        return max(self.limits.max_entries - self.used_entries, 0)

    def charge(self, files: int, size: int) -> None:
        """Record *files* entries and *size* bytes as produced.

        Parameters
        ----------
        files : int
            Number of entries the extraction created.
        size : int
            Number of bytes the extraction wrote.
        """
        self.used_entries += files
        self.used_bytes += size


def extract_archive(system: System, archive: Path, kind: str, destination: Path, budget: Budget | Limits,
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
    budget : Budget or Limits
        What may still be produced (a ``Limits`` starts a new budget).
    seven_zip : Path, optional
        The 7-Zip executable for every kind but ``zip``.

    Raises
    ------
    UnpackError
        If anything about the archive is unsafe, over a limit, or fails to extract.
    """
    # A bare Limits starts a fresh budget; then dispatch on the kind, turning every low-level failure into UnpackError.
    if isinstance(budget, Limits):
        budget = Budget(budget)
    # Zip is handled in process, tarballs in two passes, everything else by 7-Zip.
    try:
        if kind == "zip":
            _extract_zip(archive, destination, budget)
        else:
            # Every other kind goes through 7-Zip; tarballs need their own two-pass handling.
            tool = SevenZip(system, seven_zip)
            if kind == "tar":
                _extract_tar(tool, archive, destination, budget)
            else:
                _extract_with_seven_zip(tool, archive, destination, budget, kind)
    except (UnsafePath, SevenZipError, zipfile.BadZipFile, NotImplementedError, EOFError, zlib.error, lzma.LZMAError, OSError) as error:
        raise UnpackError(str(error)) from error
    # Walk what was really written and charge it to the shared budget.
    files, size = check_tree(destination, budget.limits, budget)
    budget.charge(files, size)


def check_tree(root: Path, limits: Limits, budget: Budget | None = None) -> tuple[int, int]:
    """Walk *root* without following links; return ``(file count, byte count)``.

    Parameters
    ----------
    root : Path
        The extracted tree.
    limits : Limits
        The per-file and total bounds.
    budget : Budget, optional
        When given, its remaining allowance replaces the total bounds.

    Returns
    -------
    tuple of int
        The number of files and the total bytes.

    Raises
    ------
    UnpackError
        If any entry is a link, junction or special file, or a limit (the budget's, when given)
        is exceeded.
    """
    # Work out how much the walk may find, then count every entry and byte.
    allowed_bytes = budget.bytes_left if budget is not None else limits.max_total_bytes
    allowed_entries = budget.entries_left if budget is not None else limits.max_entries
    # Every entry counts toward the entry limit, and links or junctions are refused outright.
    files = total = entries = 0
    for current, folders, names in os.walk(root, followlinks=False):
        for name in [*folders, *names]:
            entries += 1
            path = Path(current) / name
            if is_reparse_point(path):
                raise UnpackError(f"The archive produced a link or junction at {path.relative_to(root).as_posix()}")
            if entries > allowed_entries:
                raise UnpackError(f"The archive has more than {limits.max_entries} entries")
        # Files must be regular and within the size limits.
        for name in names:
            path = Path(current) / name
            status = os.lstat(path)
            if not stat.S_ISREG(status.st_mode):
                raise UnpackError(f"The archive produced a special file at {path.relative_to(root).as_posix()}")
            files += 1
            total += status.st_size
            if status.st_size > limits.max_file_bytes or total > allowed_bytes:
                raise UnpackError("The archive is larger than the allowed size when unpacked")
    return files, total


def decompress_capped(source: Path, destination: Path, limit: int) -> int:
    """Decompress a gzip, bzip2 or xz file to *destination*, never writing more than *limit* bytes.

    The xz decoder runs with a memory limit (``XZ_MEMORY_LIMIT``), so a file that asks for a huge
    dictionary fails before it allocates it. On any failure the partial output is deleted.

    Parameters
    ----------
    source : Path
        The compressed file.
    destination : Path
        The output file; it must not exist.
    limit : int
        The most bytes that may be written.

    Returns
    -------
    int
        The number of bytes written.

    Raises
    ------
    UnpackError
        If the output would exceed *limit* (the block that would cross it is not written), the
        xz file needs more memory than allowed, the file is damaged, or it is not one of the
        three formats.
    """
    # Check the signature, then stream the blocks to the output, stopping at the cap.
    with open(source, "rb") as handle:
        head = handle.read(8)
    if not any(head.startswith(signature) for signature, _opener in _DECOMPRESSORS):
        raise UnpackError(f"{source.name} is not a gzip, bzip2 or xz file")
    # Create the output exclusively and stop as soon as the cap would be crossed.
    written = 0
    try:
        with open(destination, "xb") as writer:
            for block in _decompressed_blocks(source, head):
                written += len(block)
                if written > limit:
                    raise UnpackError("The compressed file expands past the allowed size")
                writer.write(block)
    # Never leave partial output; decoder errors become UnpackError, everything else propagates.
    except BaseException as error:
        destination.unlink(missing_ok=True)
        if isinstance(error, (lzma.LZMAError, EOFError, zlib.error, gzip.BadGzipFile)):
            raise UnpackError(f"{source.name} cannot be decompressed: {error}") from error
        raise
    return written


def _xz_blocks(source: Path):
    """Yield the content of every xz stream in *source*, in blocks of at most ``_COPY_BLOCK`` bytes.

    The xz format allows several streams one after the other, separated or followed by *stream
    padding* (zero bytes, a multiple of four in all). Anything else after a stream is an error: it
    must be the header of another valid stream, which the decoder checks.
    """

    # One decoder per stream, each with the memory limit.
    def _fresh() -> lzma.LZMADecompressor:
        """Return a decoder for one stream with the memory limit (internal to the stream loop)."""
        return lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=XZ_MEMORY_LIMIT)

    with open(source, "rb") as reader:
        decompressor = _fresh()
        pending = b""
        while True:
            # At the end of a stream, skip zero padding (a multiple of four) and start the next stream.
            if decompressor.eof:
                # Skip zero padding after the stream, reading more input as needed; the end of the file ends the generator.
                rest, padding = decompressor.unused_data, 0
                while True:
                    stripped = rest.lstrip(b"\0")
                    padding += len(rest) - len(stripped)
                    if stripped:
                        break
                    # Only padding so far: read on, and finish cleanly at the end of the file.
                    rest = reader.read(_COPY_BLOCK)
                    if not rest:
                        if padding % 4:
                            raise EOFError("the xz stream padding is not a multiple of four bytes")
                        return
                # Padding must be a multiple of four bytes; what follows must be another stream.
                if padding % 4:
                    raise EOFError("the xz stream padding is not a multiple of four bytes")
                decompressor, pending = _fresh(), stripped
            # Otherwise feed the decoder more input and hand out whatever it produces.
            elif decompressor.needs_input and not pending:
                pending = reader.read(_COPY_BLOCK)
                if not pending:
                    raise EOFError("the xz stream is cut short")
            block = decompressor.decompress(pending, max_length=_COPY_BLOCK)
            pending = b""
            if block:
                yield block


def _decompressed_blocks(source: Path, head: bytes):
    """Yield the decompressed content of *source* in blocks of at most ``_COPY_BLOCK`` bytes."""
    if head.startswith(b"\xfd7zXZ\x00"):
        yield from _xz_blocks(source)
        return
    opener = next(open_ for signature, open_ in _DECOMPRESSORS if head.startswith(signature))
    with opener(source, "rb") as reader:
        while block := reader.read(_COPY_BLOCK):
            yield block


# ---------------------------------------------------------------------------
# zip, in process
# ---------------------------------------------------------------------------


def _extract_zip(archive: Path, destination: Path, budget: Budget) -> None:
    """Write every member of a zip archive below *destination*, judging each one first.

    Raises
    ------
    UnpackError
        If the archive has too many or too large members, a link, a special file, encryption,
        or a member that expands past its declared size.
    """
    # Judge the whole archive before writing: entry count, names and declared sizes.
    limits = budget.limits
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if len(infos) > budget.entries_left:
            raise UnpackError(f"The archive has {len(infos)} entries; at most {limits.max_entries} are allowed")
        names = check_member_set(info.filename for info in infos)
        declared = sum(info.file_size for info in infos)
        if declared > budget.bytes_left or any(info.file_size > limits.max_file_bytes for info in infos):
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

        # Write the members in order, each confined to the destination.
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
                    if produced > info.file_size or produced > limits.max_file_bytes or written > budget.bytes_left:
                        raise UnpackError(f"{info.filename!r} expands past its declared or the allowed size")
                    sink.write(block)


# ---------------------------------------------------------------------------
# 7-Zip based kinds
# ---------------------------------------------------------------------------


class _FolderWatch:
    """Measure a folder while 7-Zip writes into it and say when a limit is crossed.

    ``System.run`` calls it about five times a second, so it reacts *within one poll* of a limit
    being crossed, not at the moment it is crossed; the walk is repeated no more often than four
    times its own duration, so a huge tree is not scanned continuously. Links and junctions are not
    entered and not counted (a cyclic junction cannot make the walk loop), and the final tree walk
    (``check_tree``) rejects them. A bomb whose archive declares its size is stopped earlier, by the
    check of the listing; this watcher is for archives that declare nothing, or lie.
    """

    def __init__(self, root: Path, max_bytes: int, max_entries: int, max_file: int) -> None:
        """Remember the folder and the limits."""
        self.root, self.max_bytes, self.max_entries, self.max_file = root, max_bytes, max_entries, max_file
        self._next = 0.0
        self.largest = 0

    def __call__(self) -> str | None:
        """Return the reason to stop 7-Zip, or ``None`` while the folder is within its limits."""
        # Rate-limit the scan: do nothing until the back-off from the last scan has passed.
        now = time.monotonic()
        if now < self._next:
            return None

        # Walk the folder without entering links, counting entries and bytes and checking each file.
        total = entries = 0
        pending = [self.root]
        while pending:
            folder = pending.pop()
            try:
                scanner = os.scandir(folder)
            except OSError:
                continue
            # Count entries and bytes of this folder; links are not entered and a huge file stops the scan.
            with scanner:
                for entry in scanner:
                    entries += 1
                    path = Path(entry.path)
                    # Links and junctions are neither entered nor counted; folders are queued for the walk.
                    if is_reparse_point(path):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                        continue
                    # A file larger than the per-file limit stops 7-Zip at once.
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if size > self.max_file:
                        return "a file is larger than the allowed size"
                    total += size
            if total > self.max_bytes or entries > self.max_entries:
                break
        # Back off for four times this scan's duration, then report any limit that was crossed.
        self.largest = max(self.largest, total)
        self._next = time.monotonic() + 4 * (time.monotonic() - now)
        if total > self.max_bytes:
            return "the archive expands past the allowed size"
        if entries > self.max_entries:
            return "the archive has more entries than allowed"
        return None


def _unpack_watched(tool: SevenZip, archive: Path, destination: Path, budget: Budget, kind: str | None) -> None:
    """Run 7-Zip into *destination* under a watcher that enforces the budget while it works.

    Raises
    ------
    UnpackError
        If 7-Zip fails or the watcher stops it.
    """
    watch = _FolderWatch(destination, budget.bytes_left, budget.entries_left, budget.limits.max_file_bytes)
    try:
        tool.extract(archive, destination, kind, watch=watch)
    except SevenZipError as error:
        raise UnpackError(str(error)) from error


def _judge(listing: Listing, budget: Budget) -> None:
    """Refuse a table of contents with an unsafe name, a link, encryption, or a size over a limit.

    Sizes are what the archive claims, and some formats claim none: this is an early refusal,
    and the folder watcher is the real bound.

    Raises
    ------
    UnpackError
        If the listing breaks any of those rules.
    """
    # Judge the names as a set first (unsafe or colliding names), then every entry on its own.
    limits, entries = budget.limits, listing.entries
    if len(entries) > budget.entries_left:
        raise UnpackError(f"The archive has {len(entries)} entries; at most {limits.max_entries} are allowed")
    check_member_set(entry.path for entry in entries)
    check_member_set((entry.path for entry in entries if not entry.is_dir), repeats=False)
    # Per-entry rules: no encryption, no links, no file over the per-file limit.
    for entry in entries:
        if entry.encrypted:
            raise UnpackError("The archive is encrypted")
        if entry.is_link:
            raise UnpackError(f"The archive contains a link: {entry.path!r}")
        if entry.size > limits.max_file_bytes:
            raise UnpackError(f"{entry.path!r} is larger than the allowed size")
    if sum(entry.size for entry in entries) > budget.bytes_left:
        raise UnpackError("The archive declares more uncompressed data than the allowed size")


def _extract_with_seven_zip(tool: SevenZip, archive: Path, destination: Path, budget: Budget, kind: str) -> None:
    """Extract a 7z, self-extracting, NSIS or MSI file after judging its table of contents.

    Raises
    ------
    UnpackError
        If 7-Zip reports another type than the declared kind, or the listing is refused.
    """
    # List the archive (NSIS needs its format named) and check 7-Zip agrees with the declared kind.
    forced = "Nsis" if kind == "nsis" else None
    listing = tool.listing(archive, forced)
    if kind == "7z" and listing.type != "7z":
        raise UnpackError(f"{archive.name} is not a 7z archive (7-Zip reads it as {listing.type})")
    if kind == "nsis" and listing.type.lower() != "nsis":
        raise UnpackError(f"{archive.name} is not an NSIS installer (7-Zip reads it as {listing.type})")
    # Self-extractors listed as plain executables get a second listing with the 7z format named.
    if kind == "sfx" and listing.type == "PE":
        # 7-Zip reads some self-extractors (Git for Windows' PortableGit among them) as the executable they are; the archive appended
        # to the stub is found when the type is named. It is only listed and unpacked, never run.
        forced = "7z"
        listing = tool.listing(archive, forced)
        if listing.type != "7z":
            raise UnpackError(f"{archive.name} is an executable with no archive inside")
    # Judge the listing, then extract under the folder watcher.
    _judge(listing, budget)
    _unpack_watched(tool, archive, destination, budget, forced)


def _extract_tar(tool: SevenZip, archive: Path, destination: Path, budget: Budget) -> None:
    """Unpack a tarball with two passes: decompress (capped, in process when possible), then unpack the tar.

    Raises
    ------
    UnpackError
        If the wrapper holds more than one file, decompression fails, or a limit is crossed.
    """
    first = tool.listing(archive)

    # A plain tar needs only the second pass; any compressed wrapper is removed first.
    if first.type.lower() == "tar":
        _judge(first, budget)
        _unpack_watched(tool, archive, destination, budget, None)
        return
    # A compressed wrapper must hold exactly one file; pass one writes it into a holding folder.
    wrapper = destination.parent / f".{destination.name}-pass1"
    wrapper.mkdir()
    try:
        _judge(first, budget)
        if len(first.entries) > 1:
            raise UnpackError("A compressed tarball wraps exactly one file")

        # Pass one writes one file, never more than the budget allows: in process for the
        # formats Python reads, otherwise 7-Zip under the folder watcher.
        # Name the intermediate file after the archive, giving ``.tgz``-style names a ``.tar`` suffix.
        name = archive.stem if archive.suffix.lower() in (".gz", ".bz2", ".xz", ".tgz", ".txz", ".tbz2") else archive.name + ".out"
        if archive.suffix.lower() in (".tgz", ".txz", ".tbz2"):
            name = archive.stem + ".tar"
        # Pick the decompressor by signature: Python's for gzip, bzip2 and xz, 7-Zip for the rest.
        inner = wrapper / safe_file_name(name)
        with open(archive, "rb") as handle:
            head = handle.read(8)
        # Decompress in process when possible; otherwise 7-Zip must produce exactly one file.
        if any(head.startswith(signature) for signature, _opener in _DECOMPRESSORS):
            decompress_capped(archive, inner, min(budget.bytes_left, budget.limits.max_file_bytes))
        else:
            _unpack_watched(tool, archive, wrapper, budget, None)
            produced = list(wrapper.iterdir())
            if len(produced) != 1 or not produced[0].is_file() or is_reparse_point(produced[0]):
                raise UnpackError("Decompressing the tarball did not produce exactly one file")
            inner = produced[0]
        # The decompressed file is a tarball to unpack, or else the content itself.
        second = tool.listing(inner, "tar") if _looks_like_tar(inner) else None
        if second is None:
            # Not a tarball after all: the decompressed file is the content.
            shutil.move(str(inner), str(destination / safe_file_name(inner.name)))
            return
        _judge(second, budget)
        _unpack_watched(tool, inner, destination, budget, "tar")
    finally:
        shutil.rmtree(wrapper, ignore_errors=True)


def _looks_like_tar(path: Path) -> bool:
    """Return whether a file has the ``ustar`` marker (or a tar name) of a tar archive."""
    with open(path, "rb") as handle:
        head = handle.read(512)
    return head[257:262] == b"ustar" or path.suffix.lower() == ".tar"
