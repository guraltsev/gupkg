"""Copy a distribution folder into a root, without its Mark of the Web and without overwriting (design 21.4, D17, D40).

A version directory that lies outside every root (an unzipped distribution in Downloads, shaped
``<name>\\<version>``) is installed from a copy inside the mode's root. The copy reads the source as data: it
refuses a source that contains a link or junction (so a standard user cannot make an administrator copy
files from somewhere else), writes only file contents (so no ``Zone.Identifier`` stream follows), builds the
copy in the root's work folder and moves it into place in one step. An existing ``<root>\\<name>\\<version>``
is never changed: when it holds the same files as the source the result is ``identical``, otherwise
``diverged``. The result says whether any source file carried a Mark of the Web, which the engine records
in the receipt (D40).

Usage and API
-------------
Call ``copy_distribution(source, root)``; it returns a ``CopyResult`` (destination, status, whether the source
was marked). ``DistributionRefused`` reports a source that must not be copied.

Implementation Approach
-----------------------
The source tree is walked once with links detected before they are followed. Contents are streamed from the
data stream of each file into a fresh folder below ``<root>\\.gupkg\\work`` and the folder is renamed to its
final place, so an interrupted copy never leaves a half-written version directory.
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .catalog.local import DATA_DIR_NAME, is_reparse_point
from .model.version import MANIFEST_NAME, is_version_name

MARK_STREAM = "Zone.Identifier"

COPIED = "copied"
IDENTICAL = "identical"
DIVERGED = "diverged"


class DistributionRefused(ValueError):
    """Refuse to copy a source folder; the message says why."""


@dataclass(frozen=True)
class CopyResult:
    """The outcome of a copy: where it went, what happened to an existing copy, and whether the source was marked."""

    destination: Path
    status: str
    source_marked: bool
    files: int


def copy_distribution(source: Path, root: Path) -> CopyResult:
    """Copy the version directory *source* to ``<root>\\<name>\\<version>``, never overwriting.

    Parameters
    ----------
    source : Path
        A folder shaped ``<name>\\<version>`` that holds ``pkg.toml``; the parent folder's name is the package name.
    root : Path
        The root of the mode that installs it.

    Returns
    -------
    CopyResult
        ``copied``, ``identical`` (the destination already holds every file of the source) or ``diverged``
        (the destination differs and is left alone); ``source_marked`` is true when any source file had a
        Mark of the Web.

    Raises
    ------
    DistributionRefused
        If the names are unsafe, ``pkg.toml`` is missing, the source holds a link or junction, or the root
        or its work folder is a link.
    OSError
        If a file cannot be read or written.
    """
    source, root = Path(source), Path(root)
    name, version = source.parent.name, source.name

    # Validate the identity of the copy before anything is read or written.
    if not is_version_name(name) or not is_version_name(version):
        raise DistributionRefused(f"{source} is not shaped <name>\\<version>: '{name}' or '{version}' is not a safe folder name")
    if is_reparse_point(source) or not source.is_dir() or not (source / MANIFEST_NAME).is_file():
        raise DistributionRefused(f"{source} must be a real folder with a {MANIFEST_NAME}")
    for folder in (root, root / DATA_DIR_NAME):
        if is_reparse_point(folder):
            raise DistributionRefused(f"{folder} is a link; refusing to write through it")

    # Copy into a fresh work folder first; only a complete copy is moved into place.
    work = root / DATA_DIR_NAME / "work" / f"copy-{uuid.uuid4().hex}"
    staged = work / version
    try:
        staged.mkdir(parents=True)
        marked, count = _copy_tree(source, staged)
        destination = root / name / version

        # An existing copy is never overwritten: report whether it already holds this content.
        if destination.exists():
            same = _contains(destination, staged)
            return CopyResult(destination, IDENTICAL if same else DIVERGED, marked, count)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, destination)
        return CopyResult(destination, COPIED, marked, count)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _copy_tree(source: Path, target: Path) -> tuple[bool, int]:
    """Copy *source* into the existing folder *target*; return whether any file was marked and the file count."""
    marked = False
    count = 0
    stack = [(source, target)]
    while stack:
        folder, mirror = stack.pop()
        for entry in os.scandir(folder):
            path = Path(entry.path)

            # A link or junction in the source could point anywhere the copying account can read.
            if is_reparse_point(path):
                raise DistributionRefused(f"{path} is a link or junction; a distribution folder must contain only real files")
            destination = mirror / entry.name
            if entry.is_dir(follow_symlinks=False):
                destination.mkdir()
                stack.append((path, destination))
                continue

            # Only the data stream is copied, so the Mark of the Web stays behind.
            marked = marked or _is_marked(path)
            with open(path, "rb") as reader, open(destination, "xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            info = entry.stat(follow_symlinks=False)
            os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
            count += 1
    return marked, count


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
