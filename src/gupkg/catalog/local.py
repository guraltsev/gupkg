"""Scan roots and package roots on disk and build their ``LocalState``.

The module reads a root's packages (grouping folders marked by ``gupkg-dir.toml`` work as
in 0.17), each package's version directories and tracker, and what ``current`` is. It
never changes anything and prints nothing. Nothing inside a package root is followed out of
it: a version directory, tracker, ``App`` or ``pkg.toml`` that is a link is reported as
invalid instead of being read, and ``current`` must be a junction to a direct child of its
own package root, whatever spelling (letter case, ``..``, ``\\\\?\\`` prefix, 8.3 names,
trailing dots or spaces) the junction target uses.

Usage and API
-------------
Call ``scan_root(root)`` to list the packages of a root, ``scan_package(path, system)`` to
build one package's ``LocalState``, ``read_current(package_root, system)`` to inspect the
activation entry, ``read_adhoc_roots(data_dir)`` to read the ad-hoc roots recorded in a root
data directory, and ``tree_hash(path)`` for the content hash that the install record keeps
for a copied tracker. ``is_inside(path, root)`` is the containment test used throughout.

Implementation Approach
-----------------------
A version or tracker directory is validated by the authoring checks of ``gupkg.pkgcheck``
(which wraps the manifest model of ``gupkg.model.manifest``), after a link check made on
the directory entry itself. One invalid directory marks only itself invalid. Paths are
compared in a canonical form (real path, no extended-length prefix, no trailing dots or
spaces on a component, lower case) so the same folder is recognised by any spelling.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import stat
import tomllib
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path

from ..model.state import (
    CurrentState,
    LocalState,
    RecordError,
    Root,
    AdhocRoot,
    TrackerState,
    VersionState,
    parse_state,
)
from ..model.version import (
    CURRENT_DIR_NAME,
    MANIFEST_NAME,
    TRACKER_DIR_NAME,
    compare_versions,
    is_version_name,
)
from ..pkgcheck import check_directory
from ..system.port import System
from ..tomlio import read_toml

# Windows marks junctions and symbolic links with this file attribute.
_REPARSE_POINT = 0x400

# The marker file that turns a folder into a grouping folder (as in 0.17).
GROUP_MARKER = "gupkg-dir.toml"

# The folder of a root, or of a package, that holds gupkg's own files.
DATA_DIR_NAME = ".gupkg"


@dataclass(frozen=True)
class RootScan:
    """The packages found in one root: ``(name, path)`` pairs, and what could not be read."""

    packages: tuple[tuple[str, Path], ...]
    diagnostics: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Paths and links
# ---------------------------------------------------------------------------


def strip_extended_prefix(text: str) -> str:
    """Return *text* without a leading extended-length prefix (the ``?`` device prefix, also for UNC paths)."""
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def real_path(path: str | os.PathLike[str]) -> str:
    """Return the real path of *path* (links resolved) without an extended-length prefix, keeping letter case."""
    return strip_extended_prefix(os.path.realpath(os.fspath(path)))


def canonical_path(path: str | os.PathLike[str]) -> str:
    """Return a spelling of *path* that is the same for every way of naming one folder.

    Parameters
    ----------
    path : str or path-like
        Any path; it need not exist.

    Returns
    -------
    str
        The real path (links resolved) without a ``\\\\?\\`` prefix, with ``..`` removed,
        trailing dots and spaces dropped from each component and letter case folded.
    """
    drive, rest = ntpath.splitdrive(real_path(path))
    parts = [part.rstrip(" .") if part not in (".", "..") else part for part in rest.replace("/", "\\").split("\\")]
    return ntpath.normcase(drive + "\\" + "\\".join(part for part in parts if part != "")).rstrip("\\")


def is_inside(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Return whether *path* lies strictly inside the folder *root*.

    Parameters
    ----------
    path : str or path-like
        The path to test.
    root : str or path-like
        The folder that must contain it.

    Returns
    -------
    bool
        ``True`` when the canonical form of *path* is below the canonical form of *root*;
        a path equal to *root* is not inside it.
    """
    inner, outer = canonical_path(path), canonical_path(root)
    return inner.startswith(outer + "\\") and len(inner) > len(outer) + 1


def is_reparse_point(path: Path) -> bool:
    """Return whether the directory entry *path* itself is a link or junction (it is not followed)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _non_empty_dir(path: Path) -> bool:
    """Return whether *path* is a real (not linked) directory with at least one entry."""
    try:
        return path.is_dir() and not is_reparse_point(path) and any(path.iterdir())
    except OSError:
        return False


def tree_hash(path: Path) -> str:
    """Return the SHA-256 over the names and contents of every file below *path*.

    Parameters
    ----------
    path : Path
        A directory such as a copied ``_check_updates``.

    Returns
    -------
    str
        Lower-case hexadecimal digest. Links are hashed by name and never followed, and
        line endings are not normalised, so any edit changes the digest.

    Raises
    ------
    OSError
        If the directory cannot be read.
    """
    digest = hashlib.sha256()
    stack = [Path(path)]
    entries: list[tuple[str, Path]] = []
    while stack:
        folder = stack.pop()
        for child in folder.iterdir():
            relative = child.relative_to(path).as_posix().casefold()
            if is_reparse_point(child):
                entries.append((relative + "\0link", child))
            elif child.is_dir():
                stack.append(child)
            else:
                entries.append((relative, child))

    # Sorting makes the digest independent of directory order.
    for label, child in sorted(entries):
        digest.update(label.encode("utf-8") + b"\0")
        if not label.endswith("\0link"):
            digest.update(hashlib.sha256(child.read_bytes()).digest())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------


def scan_root(root: Root, *, max_depth: int = 8) -> RootScan:
    """List the packages of one root.

    Parameters
    ----------
    root : Root
        The root to scan. A root marked ``single`` is itself one package root.
    max_depth : int, default=8
        How many marked grouping folders may be descended through.

    Returns
    -------
    RootScan
        Packages in name order (none for a root that does not exist), and a diagnostic for every folder that could not be read,
        was skipped because it is a link, or was left out (grouping depth, loops).
    """
    if root.single:
        return RootScan(((root.path.name, root.path),))
    packages: list[tuple[str, Path]] = []
    diagnostics: list[str] = []
    visited = {canonical_path(root.path)}

    # A root that does not exist yet (a portable root nobody has used) simply holds no packages.
    if not root.path.is_dir():
        return RootScan((), ())

    def scan(folder: Path, prefix: str, depth: int) -> None:
        """List one folder, descending only through marked grouping folders."""
        try:
            children = sorted(os.scandir(folder), key=lambda item: (item.name.casefold(), item.name))
        except OSError as exc:
            diagnostics.append(f"Could not scan {folder}: {exc}")
            return
        for entry in children:
            child = Path(entry.path)
            name = f"{prefix}{entry.name}"

            # Gupkg's own folder and anything else starting with a dot is never a package.
            if entry.name.startswith("."):
                continue
            if is_reparse_point(child):
                diagnostics.append(f"Skipped {child}: it is a link and is not followed")
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                marker = (child / GROUP_MARKER).is_file()
                if looks_like_package(child):
                    if marker:
                        diagnostics.append(f"Layout conflict: package root {child} also contains {GROUP_MARKER}")
                    packages.append((name, child))
                    continue
            except OSError as exc:
                diagnostics.append(f"Could not inspect {child}: {exc}")
                continue
            if not marker:
                continue
            if depth >= max_depth:
                diagnostics.append(f"Maximum depth reached at grouping folder: {child}")
                continue
            key = canonical_path(child)
            if key in visited:
                diagnostics.append(f"Skipped already visited grouping folder: {child}")
                continue
            visited.add(key)
            scan(child, f"{name}/", depth + 1)

    scan(root.path, "", 0)
    return RootScan(tuple(packages), tuple(diagnostics))


def looks_like_package(path: Path) -> bool:
    """Return whether the folder *path* is a package root.

    A package root holds a version directory (a valid version name with a ``pkg.toml``), a
    tracker, a ``current`` entry or an install record. 0.17's ``_bootstrap`` is reserved
    and counts for nothing (decision D33).
    """
    try:
        names = {entry.name: entry for entry in os.scandir(path)}
    except OSError:
        return False
    if any(name.casefold() == CURRENT_DIR_NAME for name in names):
        return True
    if (path / DATA_DIR_NAME / "package.toml").is_file():
        return True
    for name, entry in names.items():
        if (is_version_name(name) or name == TRACKER_DIR_NAME) and entry.is_dir(follow_symlinks=False):
            if (path / name / MANIFEST_NAME).is_file():
                return True
    return False


def read_adhoc_roots(data_dir: Path) -> tuple[tuple[AdhocRoot, ...], str | None]:
    """Read the ad-hoc roots recorded in ``<data_dir>\\state.toml``.

    Parameters
    ----------
    data_dir : Path
        A root data directory (``<root>\\.gupkg``).

    Returns
    -------
    tuple
        ``(roots, problem)``. A missing ``state.toml`` gives no roots and no problem; an
        unreadable or invalid one gives no roots and a message.
    """
    state = Path(data_dir) / "state.toml"
    if is_reparse_point(Path(data_dir)):
        return (), f"{data_dir} is a link and is not followed"
    if not state.is_file():
        return (), None
    try:
        return parse_state(read_toml(state)), None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, RecordError) as exc:
        return (), f"Cannot read {state}: {exc}"


# ---------------------------------------------------------------------------
# One package root
# ---------------------------------------------------------------------------


def read_current(package_root: Path, system: System) -> CurrentState:
    """Inspect the ``current`` entry of a package root without choosing a fallback.

    Parameters
    ----------
    package_root : Path
        The package root.
    system : System
        Boundary that reads the junction.

    Returns
    -------
    CurrentState
        ``valid`` only for a junction whose target is a direct child of *package_root*
        with a valid version name and a ``pkg.toml``, so a junction (or a user-writable
        install record) can never lead an elevated run out of the package root.
    """
    entry = Path(package_root) / CURRENT_DIR_NAME
    if not os.path.lexists(entry):
        return CurrentState("absent")
    if not system.is_link(entry):
        return CurrentState("not-a-link", problem=f'"current" is not a junction: {entry}')
    target = system.read_link(entry)
    if target is None:
        return CurrentState("dangling", problem=f'Could not resolve "current": {entry}')

    # The target must be one of this package root's own folders, whatever its spelling.
    canonical_target = canonical_path(target)
    if ntpath.dirname(canonical_target) != canonical_path(package_root):
        return CurrentState("escapes", target=target, problem=f'"current" points outside the package root: {target}')
    if not target.is_dir():
        return CurrentState("dangling", target=target, problem=f'"current" target is missing: {target}')
    version = Path(os.path.realpath(target)).name
    if not is_version_name(version) or not (target / MANIFEST_NAME).is_file():
        return CurrentState("bad-target", target=target, problem=f'"current" target is not a version directory: {target}')
    return CurrentState("valid", version=version, target=target)


def scan_package(path: Path, system: System) -> LocalState:
    """Build the ``LocalState`` of the package root *path*.

    Parameters
    ----------
    path : Path
        The package root.
    system : System
        Boundary that reads ``current``.

    Returns
    -------
    LocalState
        Version directories (oldest first), the tracker and ``current``. Each version and
        the tracker is validated on its own: an invalid one carries its problems and does
        not stop the others.
    """
    path = Path(path)
    versions: list[VersionState] = []
    tracker: TrackerState | None = None
    try:
        children = sorted(os.scandir(path), key=lambda item: item.name)
    except OSError as exc:
        return LocalState(path, current=CurrentState("dangling", problem=f"Cannot read {path}: {exc}"))

    # Reserved names (current, data, anything starting with a dot or an underscore) are not versions.
    for entry in children:
        child = Path(entry.path)
        if entry.name == TRACKER_DIR_NAME:
            tracker = _scan_tracker(child)
        elif is_version_name(entry.name):
            if is_reparse_point(child):
                versions.append(VersionState(entry.name, child, False, False, (f"{entry.name} is a link and is not followed",)))
            elif entry.is_dir(follow_symlinks=False) and (child / MANIFEST_NAME).is_file():
                versions.append(_scan_version(child))

    # Oldest first, by the version ordering of 0.17.
    versions.sort(key=cmp_to_key(lambda left, right: compare_versions(left.name, right.name)))
    return LocalState(path, tuple(versions), tracker, read_current(path, system))


def manifest_text_hash(manifest: Path) -> str | None:
    """Return the SHA-256 of a ``pkg.toml`` with line endings normalised, or ``None`` if unreadable."""
    try:
        text = manifest.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _link_problems(directory: Path, names: tuple[str, ...]) -> list[str]:
    """Return a problem for each of the named children of *directory* that is a link."""
    return [f"{name} is a link and is not followed" for name in names if is_reparse_point(directory / name)]


def _scan_version(directory: Path) -> VersionState:
    """Validate one version directory; a link inside it is a problem and is never read."""
    problems = _link_problems(directory, (MANIFEST_NAME, "App", "pkg.local"))
    manifest = None
    if not problems:
        try:
            report = check_directory(directory)
        except (OSError, ValueError) as exc:
            problems = [f"Cannot check {directory}: {exc}"]
        else:
            problems = list(report.errors)
            manifest = report.manifest
    return VersionState(
        directory.name,
        directory,
        pinned=manifest is not None,
        populated=_non_empty_dir(directory / "App"),
        problems=tuple(problems),
        manifest=manifest,
        pkg_toml_sha256=None if is_reparse_point(directory / MANIFEST_NAME) else manifest_text_hash(directory / MANIFEST_NAME),
    )


def _scan_tracker(directory: Path) -> TrackerState:
    """Validate the tracker directory; its content hash is the one the install record compares."""
    if is_reparse_point(directory):
        return TrackerState(directory, (f"{TRACKER_DIR_NAME} is a link and is not followed",))
    if not (directory / MANIFEST_NAME).is_file():
        return TrackerState(directory, (f"{TRACKER_DIR_NAME} has no {MANIFEST_NAME}",))
    problems = _link_problems(directory, (MANIFEST_NAME, "pkg.local"))
    manifest = None
    if not problems:
        try:
            report = check_directory(directory)
        except (OSError, ValueError) as exc:
            problems = [f"Cannot check {directory}: {exc}"]
        else:
            problems = list(report.errors)
            manifest = report.manifest
    try:
        digest = tree_hash(directory)
    except OSError:
        digest = None
    return TrackerState(directory, tuple(problems), manifest, digest)
