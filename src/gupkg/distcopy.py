"""Copy a distribution folder into a root, without its Mark of the Web and without overwriting (design 21.4, D17, D40).

A version directory that lies outside every root (an unzipped distribution in Downloads, shaped
``<name>\\<version>``) is installed from a copy inside the mode's root. The source is only data that a
standard user may be changing while it is read: the copy refuses a source that contains a link or
junction, re-checks every directory and file at the moment it is opened (a swap for a link between the
check and the use is detected and refused), refuses hostile names (reserved device names, trailing dots
and spaces, stream syntax) up front, writes only the data stream of each file (every alternate stream, the
Mark of the Web included, stays behind), builds the copy in the root's work folder and moves it into place
in one step. An existing ``<root>\\<name>\\<version>`` is never changed: when it holds the same files the
result is ``identical``, otherwise ``diverged``. The result says whether any source file carried a Mark of
the Web, which the engine records in the receipt (D40). A failed or refused copy leaves the root as it was.

Usage and API
-------------
Call ``copy_distribution(source, root, required=...)``; it returns a ``CopyResult`` (destination, status,
whether the source was marked). ``required`` is the ``ModeChoice`` the install will use: the staged
``pkg.toml``'s ``modes`` are checked before anything is moved into place. ``DistributionRefused`` (exit
code 2) reports a source that must not be copied.

Implementation Approach
-----------------------
The source tree is walked once. A directory is listed only after it is shown to be a plain directory, and
the listing is accepted only if the directory is still the same one afterwards. A file is opened first, and
the open handle must be the very file that the path names (same identity, not a link) and must still lie
inside the source. The copy lives below ``<root>\\.gupkg\\work`` and the folder is renamed to its final
place, so an interrupted copy never leaves a half-written version directory.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from .catalog.local import DATA_DIR_NAME, canonical_path, is_inside, is_reparse_point
from .model.manifest import ManifestError, parse_manifest
from .model.mode import ModeChoice, ModeRefused, check_supported
from .model.version import MANIFEST_NAME, is_version_name

MARK_STREAM = "Zone.Identifier"

COPIED = "copied"
IDENTICAL = "identical"
DIVERGED = "diverged"

# Windows device names are reserved with or without an extension; the superscript digits count too.
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{device}{digit}" for device in ("COM", "LPT") for digit in "123456789¹²³"}
)
_ILLEGAL_CHARACTERS = re.compile(r'[<>:"/\\|?*]|[\x00-\x1f]')


class DistributionRefused(ModeRefused):
    """Refuse to copy a source folder (exit code 2); the message says why."""


@dataclass(frozen=True)
class CopyResult:
    """The outcome of a copy: where it went, what happened to an existing copy, and whether the source was marked."""

    destination: Path
    status: str
    source_marked: bool
    files: int


def copy_distribution(source: Path, root: Path, *, required: ModeChoice | None = None) -> CopyResult:
    """Copy the version directory *source* to ``<root>\\<name>\\<version>``, never overwriting.

    Parameters
    ----------
    source : Path
        A folder shaped ``<name>\\<version>`` that holds ``pkg.toml``; the parent folder's name is the package name.
    root : Path
        The root of the mode that installs it.
    required : ModeChoice, optional
        The mode the install will use. The staged ``pkg.toml`` must list it in ``modes``; otherwise the copy is
        refused (``ModeRefused``) before anything is moved into the root.

    Returns
    -------
    CopyResult
        ``copied``, ``identical`` (the destination already holds every file of the source) or ``diverged``
        (the destination differs and is left alone); ``source_marked`` is true when any source file had a
        Mark of the Web.

    Raises
    ------
    DistributionRefused
        If a name is unsafe, ``pkg.toml`` is missing or invalid, the source holds (or is swapped for) a link or
        junction, or the root or its work folder is a link.
    ModeRefused
        If *required* is not in the package's ``modes``.
    OSError
        If a file cannot be read or written.
    """
    source, root = Path(source), Path(root)
    name, version = source.parent.name, source.name

    # Validate the identity of the copy before anything is read or written.
    for part in (name, version):
        problem = _name_problem(part)
        if problem is not None or not is_version_name(part):
            raise DistributionRefused(
                f"{source} is not shaped <name>\\<version>: '{part}' is not a safe folder name"
                + (f" ({problem})" if problem else "")
            )
    if is_reparse_point(source) or not source.is_dir() or not (source / MANIFEST_NAME).is_file():
        raise DistributionRefused(f"{source} must be a real folder with a {MANIFEST_NAME}")
    for folder in (root, root / DATA_DIR_NAME):
        if is_reparse_point(folder):
            raise DistributionRefused(f"{folder} is a link; refusing to write through it")

    # Remember what did not exist, so a refused or failed copy leaves the root as it was.
    created = [folder for folder in (root, root / DATA_DIR_NAME, root / DATA_DIR_NAME / "work") if not folder.exists()]
    work = root / DATA_DIR_NAME / "work" / f"copy-{uuid.uuid4().hex}"
    staged = work / version
    try:
        staged.mkdir(parents=True)
        marked, count = _copy_tree(source, staged)

        # Check the package's own modes on the staged copy, before anything reaches the root.
        if required is not None:
            try:
                modes = parse_manifest((staged / MANIFEST_NAME).read_text(encoding="utf-8-sig")).modes
            except (OSError, UnicodeDecodeError, ManifestError, ValueError) as exc:
                raise DistributionRefused(f"{source / MANIFEST_NAME} is not a valid pkg.toml: {exc}") from exc
            check_supported(required, modes)

        # An existing copy is never overwritten: report whether it already holds this content.
        destination = root / name / version
        if destination.exists():
            same = _contains(destination, staged)
            return CopyResult(destination, IDENTICAL if same else DIVERGED, marked, count)
        parent_existed = destination.parent.exists()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staged, destination)
        except OSError:
            if not parent_existed:
                shutil.rmtree(destination.parent, ignore_errors=True)
            raise
        return CopyResult(destination, COPIED, marked, count)
    finally:
        shutil.rmtree(work, ignore_errors=True)

        # Remove the folders this call created, innermost first, but only while they are empty.
        for folder in reversed(created):
            try:
                folder.rmdir()
            except OSError:
                break


def _name_problem(name: str) -> str | None:
    """Return why *name* cannot be a file or folder name on Windows, or ``None`` when it can."""
    if name != name.rstrip(" .") or name != name.lstrip(" "):
        return "it starts with a space or ends with a space or a dot"
    if _ILLEGAL_CHARACTERS.search(name):
        return "it has a character Windows does not allow, or stream syntax"
    if name.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES:
        return "it is a reserved device name"
    return None


def _copy_tree(source: Path, target: Path) -> tuple[bool, int]:
    """Copy *source* into the existing folder *target*; return whether any file was marked and the file count."""
    marked = False
    count = 0
    stack = [(source, target)]
    while stack:
        folder, mirror = stack.pop()

        # List a directory only when it is a plain directory inside the source, and accept the listing only
        # if it is still the same directory afterwards: a swap for a junction in between is refused.
        _require_plain(folder, source)
        before = os.lstat(folder)
        entries = list(os.scandir(folder))
        after = os.lstat(folder)
        if not os.path.samestat(before, after):
            raise DistributionRefused(f"{folder} changed while it was being read; refusing the copy")
        _require_plain(folder, source)

        for entry in entries:
            path = Path(entry.path)
            problem = _name_problem(entry.name)
            if problem is not None:
                raise DistributionRefused(f"{path} cannot be copied: {problem}")

            # A link or junction in the source could point anywhere the copying account can read.
            if is_reparse_point(path):
                raise DistributionRefused(f"{path} is a link or junction; a distribution folder must contain only real files")
            destination = mirror / entry.name
            _require_inside(mirror, target)
            if entry.is_dir(follow_symlinks=False):
                destination.mkdir()
                stack.append((path, destination))
                continue
            marked = _copy_file(path, destination, source) or marked
            count += 1
    return marked, count


def _copy_file(path: Path, destination: Path, source: Path) -> bool:
    """Copy the data stream of one source file; return whether it carried a Mark of the Web.

    The file is opened first. The handle must be the very file that the path names (so the path was not
    swapped for a link after the check) and the path must still lead inside the source.
    """
    marked = _is_marked(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(path)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, named) or is_reparse_point(path):
            raise DistributionRefused(f"{path} changed while it was being copied (it is not the file that was checked)")
        _require_inside(path, source)

        # Stream only the data stream, into a file that this call creates inside the staging folder.
        with os.fdopen(descriptor, "rb", closefd=False) as reader, open(destination, "xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
    finally:
        os.close(descriptor)
    os.utime(destination, ns=(named.st_atime_ns, named.st_mtime_ns))
    return marked


def _require_plain(folder: Path, source: Path) -> None:
    """Require *folder* to be a real directory (not a link) that lies inside, or is, the source."""
    if is_reparse_point(folder):
        raise DistributionRefused(f"{folder} is a link or junction; a distribution folder must contain only real files")
    if folder != source:
        _require_inside(folder, source)


def _require_inside(path: Path, root: Path) -> None:
    """Require the real path of *path* to lie inside *root* (or be it), however it is reached."""
    if canonical_path(path) != canonical_path(root) and not is_inside(path, root):
        raise DistributionRefused(f"{path} leads outside {root}; refusing the copy")


def _is_marked(path: Path) -> bool:
    """Return whether the file carries a ``Zone.Identifier`` stream (it was downloaded and marked by Windows)."""
    try:
        with open(f"{path}:{MARK_STREAM}", "rb"):
            return True
    except OSError:
        return False


def _contains(existing: Path, copy: Path) -> bool:
    """Return whether *existing* holds every file of *copy* with the same content (it may hold more)."""
    for folder, _, files in os.walk(copy):
        relative = Path(folder).relative_to(copy)
        for name in files:
            other = existing / relative / name
            if not other.is_file() or other.read_bytes() != (Path(folder) / name).read_bytes():
                return False
    return True
