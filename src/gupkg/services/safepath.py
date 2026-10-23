"""Decide which names and paths from untrusted input may touch the file system.

Archive member names, response file names and package-supplied relative paths all
pass through these checks before any file is created. A name is accepted only when it
is relative, has no drive, stream (``:``), parent (``..``) or reserved device
segment, cannot alias another name on Windows (trailing dot or space, letter case),
and stays inside its root once joined, with no link or junction on the way.

Usage and API
-------------
``check_member_name(name)`` normalises one archive member name and raises
``UnsafePath``. ``check_member_set(names)`` additionally rejects duplicates and
names that collide when Windows folds letter case. ``confine(root, relative)``
joins a relative path to a root and proves that the result is inside the root
without crossing a link. ``safe_file_name(name)`` turns an untrusted file name (a
``Content-Disposition`` value, the last URL segment) into a harmless one.
``is_reparse_point(path)`` and ``remove_tree(path)`` handle links without following
them.

Implementation Approach
-----------------------
Names are judged as text first, with Windows rules applied on every platform, so a
hostile archive is refused the same way wherever the tests run. Paths already on
disk are judged by ``lstat`` component by component, never by resolving links, so a
planted junction cannot redirect a later write.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Iterable
from pathlib import Path

# Windows device names; a file called ``con.txt`` is still the console device.
RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)

_MAX_SEGMENT = 255
_MAX_PATH_TEXT = 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FORBIDDEN_IN_FILE_NAME = re.compile(r'[<>:"|?*\x00-\x1f\\/]')


class UnsafePath(ValueError):
    """Report a name or path that must not be used for a file."""


def check_member_name(name: str) -> str:
    """Return the normalised, relative, ``/``-separated form of an archive member name.

    Parameters
    ----------
    name : str
        The member name as stored in the archive. A trailing separator, which marks
        a directory, is allowed and removed.

    Returns
    -------
    str
        The name with ``/`` separators, no empty or ``.`` segments and no trailing
        separator.

    Raises
    ------
    UnsafePath
        If the name is empty, absolute, has a drive or ``:``, a ``..`` segment, a
        control character, a Windows device name, a segment ending in a dot or space,
        or is longer than the supported limits.
    """
    # Reject what no member name may contain before looking at its structure.
    if not name or any(ord(character) < 32 for character in name):
        raise UnsafePath(f"unsafe member name {name!r}: empty or has a control character")
    if len(name) > _MAX_PATH_TEXT:
        raise UnsafePath(f"unsafe member name {name[:40]!r}...: longer than {_MAX_PATH_TEXT} characters")
    if ":" in name:
        raise UnsafePath(f"unsafe member name {name!r}: a drive letter or stream marker ':' is not allowed")
    normal = name.replace("\\", "/")
    if normal.startswith("/"):
        raise UnsafePath(f"unsafe member name {name!r}: it is absolute")

    # Judge every segment on its own: parent steps, device names and names that
    # Windows would silently rewrite.
    segments = [segment for segment in normal.split("/") if segment not in ("", ".")]
    if not segments:
        raise UnsafePath(f"unsafe member name {name!r}: it names no file")
    for segment in segments:
        if segment == "..":
            raise UnsafePath(f"unsafe member name {name!r}: it leaves the destination")
        if segment.endswith((".", " ")):
            raise UnsafePath(f"unsafe member name {name!r}: a segment ends in a dot or space")
        if segment.split(".")[0].rstrip().casefold() in RESERVED_DEVICE_NAMES:
            raise UnsafePath(f"unsafe member name {name!r}: it uses the reserved device name {segment!r}")
        if len(segment) > _MAX_SEGMENT:
            raise UnsafePath(f"unsafe member name {name[:40]!r}...: a segment is longer than {_MAX_SEGMENT}")
        if any(character in segment for character in '<>"|?*'):
            raise UnsafePath(f"unsafe member name {name!r}: a segment has a character Windows refuses")
    return "/".join(segments)


def check_member_set(names: Iterable[str]) -> list[str]:
    """Normalise every member name and reject duplicates and case collisions.

    Parameters
    ----------
    names : iterable of str
        Member names of one archive, files and directories alike. A name may repeat
        as a directory entry (``a/`` then ``a/b``); two entries for the same file may not.

    Returns
    -------
    list of str
        The normalised names in the given order.

    Raises
    ------
    UnsafePath
        If any name is unsafe, or two different names fold to the same path on a
        case-insensitive file system.
    """
    seen: dict[str, str] = {}
    result: list[str] = []
    for raw in names:
        normal = check_member_name(raw)
        key = normal.casefold()
        previous = seen.get(key)
        if previous is not None and previous != normal:
            raise UnsafePath(f"unsafe archive: {previous!r} and {normal!r} are the same name on Windows")
        seen[key] = normal
        result.append(normal)
    return result


def is_reparse_point(path: Path) -> bool:
    """Return whether *path* is a symbolic link, junction or other reparse point (it is not followed)."""
    try:
        status = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(status.st_mode):
        return True
    return bool(getattr(status, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def confine(root: Path, relative: str, *, allow_link_leaf: bool = False) -> Path:
    """Join *relative* to *root* and prove the result stays inside it, crossing no link.

    Parameters
    ----------
    root : Path
        An existing directory that confines the result.
    relative : str
        A path from a manifest or archive; ``/`` and ``\\`` both separate segments.
    allow_link_leaf : bool
        Accept a link or junction as the last segment (to remove or rename the link
        itself); a link in any earlier segment is always refused.

    Returns
    -------
    Path
        ``root`` joined with the normalised segments. The path need not exist.

    Raises
    ------
    UnsafePath
        If *relative* is unsafe (see ``check_member_name``), or an existing part of
        the path, below *root*, is a link or junction.
    """
    normal = check_member_name(relative)
    current = Path(root)

    # Walk down one segment at a time with ``lstat`` so a link planted below the
    # root is detected, not followed.
    segments = normal.split("/")
    for position, segment in enumerate(segments):
        current = current / segment
        if is_reparse_point(current) and not (allow_link_leaf and position == len(segments) - 1):
            raise UnsafePath(f"{relative!r} passes through a link or junction at {segment!r}")
    return Path(root).joinpath(*normal.split("/"))


def safe_file_name(name: str, default: str = "download") -> str:
    """Return a harmless file name derived from untrusted text.

    Parameters
    ----------
    name : str
        A name from a header or URL. Directory parts are dropped.
    default : str
        The result when nothing usable remains.

    Returns
    -------
    str
        A single file name without separators, ``:`` or other characters Windows
        refuses, never a reserved device name, never ending in a dot or space.
    """
    last = re.split(r"[\\/]", name)[-1]
    cleaned = _FORBIDDEN_IN_FILE_NAME.sub("_", last).strip().rstrip(". ")
    if not cleaned or cleaned in (".", ".."):
        return default
    if cleaned.split(".")[0].rstrip().casefold() in RESERVED_DEVICE_NAMES:
        cleaned = "_" + cleaned
    return cleaned[:120]


def remove_tree(path: Path) -> None:
    """Delete *path* and everything below it without following any link.

    A link or junction is removed itself and its target is left alone. Read-only
    files are made writable first. A missing path is not an error.
    """
    target = Path(path)
    if is_reparse_point(target):
        try:
            os.rmdir(target)
        except OSError:
            os.unlink(target)
        return
    if not target.exists():
        return
    if target.is_file():
        os.chmod(target, stat.S_IWRITE)
        target.unlink()
        return

    def clear_and_retry(function, failed, _error) -> None:
        """Make the failing entry writable and try the removal once more."""
        if is_reparse_point(Path(failed)):
            try:
                os.rmdir(failed)
            except OSError:
                os.unlink(failed)
            return
        os.chmod(failed, stat.S_IWRITE)
        function(failed)

    # ``shutil.rmtree`` does not recurse into junctions on Windows; the handler
    # covers read-only entries and links met on the way.
    shutil.rmtree(target, onexc=clear_and_retry)
