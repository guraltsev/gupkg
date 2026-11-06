"""List and extract archives with the bundled 7-Zip, through ``System.run``.

The wrapper never lets 7-Zip decide what is safe. ``listing`` reads the archive's table
of contents (``7z l -slt``) so the caller can check every name, size and link before
anything is written; ``extract`` then unpacks into a folder that the caller owns and
re-checks afterwards. Only a zero exit status counts as success, so a damaged archive
cannot produce a half-trusted result.

Usage and API
-------------
``SevenZip(system, executable)`` is built once per unpack. ``listing(archive, kind)``
returns a ``Listing`` (archive type, SFX offset and entries) and ``extract(archive, dest,
kind)`` unpacks. ``kind`` forces a 7-Zip format such as ``"Nsis"`` or ``"tar"``.

Implementation Approach
-----------------------
The listing text is parsed as ``Key = Value`` blocks. Links are recognised from the
``Symbolic Link`` and ``Hard Link`` fields (tar), a ``l`` mode (Unix attributes) and the
reparse attribute (Windows attributes). Passwords are never supplied and standard input
is closed, so an encrypted archive fails instead of waiting for a prompt.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ...extractors import find_7z
from ...report.events import subprocess_lines
from ...system.port import ProcessAborted, System

# Seconds allowed for listing an archive.
LIST_TIMEOUT = 300.0
# Seconds allowed for extracting an archive.
EXTRACT_TIMEOUT = 3600.0
# One ``Key = Value`` line of 7-Zip's technical listing.
_FIELD = re.compile(r"^(?P<key>[A-Za-z][A-Za-z ]*?) = ?(?P<value>.*)$")
# A Unix mode string that marks a symbolic link (``lrwxrwxrwx``).
_UNIX_LINK_MODE = re.compile(r"(?:^|\s)l[rwx-]{9}\b")


class SevenZipError(RuntimeError):
    """Report that 7-Zip failed or that an archive's table of contents is unusable."""


@dataclass(frozen=True)
class Entry:
    """One item of an archive's table of contents.

    Attributes
    ----------
    path : str
        The path inside the archive.
    is_dir : bool
        Whether the entry is a folder.
    size : int
        The size in bytes.
    is_link : bool
        Whether the entry is a link.
    encrypted : bool
        Whether the entry is encrypted.
    """

    path: str
    is_dir: bool
    size: int
    is_link: bool
    encrypted: bool


@dataclass(frozen=True)
class Listing:
    """An archive's type, where its data starts (``offset`` > 0 for a self-extractor) and its entries.

    Attributes
    ----------
    type : str
        The archive type.
    offset : int
        Where the data starts.
    entries : tuple of Entry
        The table of contents.
    """

    type: str
    offset: int
    entries: tuple[Entry, ...]


class SevenZip:
    """Run the bundled 7-Zip for one unpack.

    Parameters
    ----------
    system : System
        The boundary every child process goes through.
    executable : Path, optional
        The 7-Zip executable; the bundled ``7z.exe`` (or a system one) when omitted.

    Methods
    -------
    listing
        Return the table of contents of *archive*, parsed as format *kind* when given.
    extract
        Unpack *archive* into the existing, empty folder *destination*.
    """

    def __init__(self, system: System, executable: Path | None = None) -> None:
        """Remember the boundary and resolve the executable."""
        self.system = system
        try:
            self.executable = Path(executable) if executable is not None else find_7z()
        except FileNotFoundError as error:
            raise SevenZipError(str(error)) from error

    def listing(self, archive: Path, kind: str | None = None) -> Listing:
        """Return the table of contents of *archive*, parsed as format *kind* when given.

        Parameters
        ----------
        archive : Path
            The archive to list.
        kind : str, optional
            A 7-Zip format name that overrides detection (for example ``"Nsis"``).

        Returns
        -------
        Listing
            The archive type, data offset and entries.

        Raises
        ------
        SevenZipError
            If 7-Zip cannot open the archive.
        """
        # Ask for the technical listing; a non-zero exit status means the archive is unusable.
        argv = [str(self.executable), "l", "-slt", "-sccUTF-8"]
        if kind:
            argv.append(f"-t{kind}")
        argv += ["--", str(archive)]
        result = self.system.run(argv, timeout=LIST_TIMEOUT)
        if result.returncode != 0:
            raise SevenZipError(_failure("list", archive, result.stdout, result.stderr, result.returncode))
        return parse_listing(result.stdout)

    def extract(self, archive: Path, destination: Path, kind: str | None = None,
                watch: Callable[[], str | None] | None = None) -> None:
        """Unpack *archive* into the existing, empty folder *destination*.

        7-Zip is killed when *watch* returns text.

        Parameters
        ----------
        archive : Path
            The archive to extract.
        destination : Path
            The existing, empty folder that receives the files.
        kind : str, optional
            A 7-Zip format name that overrides detection.
        watch : callable, optional
            Polled while 7-Zip runs; returning text stops the extraction with that reason.

        Raises
        ------
        SevenZipError
            If 7-Zip exits with any status but zero, or is killed because *watch* asked.
        """
        # Extract with overwrite allowed (the folder is empty); only a zero exit status is success.
        argv: list[str] = [str(self.executable), "x", "-y", f"-o{destination}", "-sccUTF-8"]
        if kind:
            argv.append(f"-t{kind}")
        argv += ["--", str(archive)]
        try:
            result = self.system.run(argv, on_line=subprocess_lines(argv), timeout=EXTRACT_TIMEOUT, watch=watch)
        except ProcessAborted as error:
            raise SevenZipError(f"7-Zip was stopped: {error}") from error
        if result.returncode != 0:
            raise SevenZipError(_failure("extract", archive, result.stdout, result.stderr, result.returncode))


def parse_listing(text: str) -> Listing:
    """Parse the output of ``7z l -slt`` into a ``Listing``.

    Parameters
    ----------
    text : str
        The standard output of ``7z l -slt``.

    Returns
    -------
    Listing
        The archive type, data offset and entries.

    Raises
    ------
    SevenZipError
        If the output has no archive header.

    Examples
    --------
    Basic usage:

    >>> t = 'Path = a.zip\\nType = zip\\n\\n----------\\nPath = x.txt\\nSize = 5\\n'
    >>> parse_listing(t)
    Listing(type='zip', offset=0, entries=(Entry(path='x.txt', is_dir=False, size=5, is_link=False, encrypted=False),))
    """
    # Split the output at the dashed line into the archive header and the entry blocks.
    lines = text.splitlines()
    try:
        start = lines.index("----------")
    except ValueError:
        header_lines, body = lines, []
    else:
        header_lines, body = lines[:start], lines[start + 1 :]

    # The header block describes the archive: its type and where its data starts.
    header = _fields(header_lines)
    kind = header.get("Type")
    if kind is None:
        raise SevenZipError("7-Zip did not report an archive type")
    offset = int(header["Offset"]) if header.get("Offset", "").isdigit() else 0

    # Each following blank-line separated block is one entry.
    entries: list[Entry] = []
    block: list[str] = []
    # A trailing blank line flushes the last block.
    for line in [*body, ""]:
        if line.strip():
            block.append(line)
            continue
        if block:
            entry = _entry(_fields(block))
            if entry is not None:
                entries.append(entry)
            block = []
    return Listing(kind, offset, tuple(entries))


def _fields(lines: Sequence[str]) -> dict[str, str]:
    """Return the ``Key = Value`` pairs of a block; the first occurrence of a key wins (internal listing parser)."""
    found: dict[str, str] = {}
    for line in lines:
        match = _FIELD.match(line)
        if match and match.group("key") not in found:
            found[match.group("key")] = match.group("value").strip()
    return found


def _entry(fields: dict[str, str]) -> Entry | None:
    """Return an ``Entry`` for one block, or ``None`` for a block without a path such as an xz stream (internal listing parser)."""
    # A block without a path describes a stream, not an entry.
    path = fields.get("Path")
    if path is None:
        return None
    attributes = fields.get("Attributes", "")
    token = attributes.split()[0] if attributes.split() else ""

    # Attributes come as letters (``D``, ``AL``), as eight hex digits whose low bits are
    # the Windows attributes and whose high half is the Unix mode (zip), or with a Unix
    # mode string (``lrwxrwxrwx``).
    is_dir = fields.get("Folder") == "+"
    is_link = bool(fields.get("Symbolic Link") or fields.get("Hard Link") or _UNIX_LINK_MODE.search(fields.get("Mode", "") + " " + attributes))
    if re.fullmatch(r"[0-9A-Fa-f]{8}", token):
        value = int(token, 16)
        is_dir = is_dir or bool(value & 0x10)
        is_link = is_link or bool(value & 0x400) or (value >> 16) & 0o170000 == 0o120000
    else:
        letters = re.sub(r"[^A-Za-z]", "", token)
        is_dir = is_dir or "D" in letters
        is_link = is_link or "L" in letters
    # The size is absent for folders and some links.
    size = int(fields["Size"]) if fields.get("Size", "").isdigit() else 0
    return Entry(path, is_dir, size, is_link, fields.get("Encrypted") == "+")


def _failure(action: str, archive: Path, stdout: str, stderr: str, status: int) -> str:
    """Return the message for a failed 7-Zip run, with the last useful output line."""
    detail = next((line.strip() for line in reversed((stderr + "\n" + stdout).splitlines()) if line.strip()), "")
    return f"7-Zip could not {action} {Path(archive).name} (exit status {status}){': ' + detail[:200] if detail else ''}"
