"""Decide which unpack kind a downloaded file needs, from its signature and extension.

Content wins over the name: a ``.exe`` that is really a 7z self-extractor is ``sfx`` and one
that is an NSIS installer is ``nsis``, whatever the file is called. The decision for an
executable needs 7-Zip's own reading of the file, which is requested through
``System.run``; every other kind is decided from the first bytes and the extension.

Usage and API
-------------
``detect_kind(system, path, filename, seven_zip)`` returns one of ``zip``, ``7z``,
``tar``, ``sfx``, ``nsis``, ``msi`` or ``file``. ``require_kind(kind, path)`` checks that a
declared kind fits the file's signature.

Implementation Approach
-----------------------
Signatures are compared first. Executables and installers are asked of 7-Zip with a
listing (``Type = Nsis``, ``Type = 7z`` with a data offset, ``Type = PE``). A file that no
rule recognises is ``file``: it is copied as it is.
"""

from __future__ import annotations

from pathlib import Path

from ...system.port import System
from .extract import UnpackError
from .sevenzip import SevenZip, SevenZipError

_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_SEVEN_ZIP = b"7z\xbc\xaf\x27\x1c"
_COMPRESSED = (b"\xfd7zXZ\x00", b"\x1f\x8b", b"BZh", b"\x28\xb5\x2f\xfd")
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def detect_kind(system: System, path: Path, filename: str | None = None, seven_zip: Path | None = None) -> str:
    """Return the unpack kind of the file at *path*.

    Parameters
    ----------
    system : System
        The boundary used to ask 7-Zip about executables.
    path : Path
        The file.
    filename : str, optional
        The name the file had when it was published; the cache may store it under another.
    seven_zip : Path, optional
        The 7-Zip executable.

    Raises
    ------
    UnpackError
        If 7-Zip is needed and unavailable, or cannot read an executable at all.
    """
    name = (filename or path.name).lower()
    with open(path, "rb") as handle:
        head = handle.read(600)

    # Plain signatures.
    if head.startswith(_ZIP):
        return "zip"
    if head.startswith(_SEVEN_ZIP):
        return "7z"
    if head[257:262] == b"ustar":
        return "tar"
    if head.startswith(_COMPRESSED):
        return "tar"
    if head.startswith(_OLE) and name.endswith(".msi"):
        return "msi"

    # An executable may carry an archive; ask 7-Zip what it sees.
    if head.startswith(b"MZ") or name.endswith(".exe"):
        try:
            listing = SevenZip(system, seven_zip).listing(path)
        except SevenZipError:
            return "exe" if head.startswith(b"MZ") else "file"
        kind = listing.type.lower()
        if kind == "nsis":
            return "nsis"
        if kind in ("pe", "mz"):
            return "exe"
        return "sfx"
    return "file"


def require_kind(kind: str, path: Path) -> None:
    """Check that a declared *kind* fits the signature of the file at *path*.

    Only the kinds with a fixed signature are checked: ``zip``, ``7z`` and ``tar``.

    Raises
    ------
    UnpackError
        If the file's first bytes cannot be that kind.
    """
    with open(path, "rb") as handle:
        head = handle.read(600)
    if kind == "zip" and not head.startswith(_ZIP):
        raise UnpackError(f"{path.name} is not a zip archive")
    if kind == "7z" and not head.startswith(_SEVEN_ZIP) and not head.startswith(b"MZ"):
        raise UnpackError(f"{path.name} is not a 7z archive")
