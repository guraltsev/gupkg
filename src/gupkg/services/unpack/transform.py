"""Reshape an unpacked tree: strip wrapper folders, keep a subtree, map globs, and check it.

These are the options every unpack kind shares. They work on a folder that the unpack owns
(never on ``App`` directly) and every path is confined to that folder: relative, no
``..``, and no link on the way. A failed check raises ``UnpackError``; the owner deletes
the folder, so nothing partial is left behind.

Usage and API
-------------
``strip_levels(root, levels)``, ``keep_subdir(root, subdir)``, ``map_extract(root, rules,
scratch)``, ``merge_into(source, destination, replace)``, ``require_paths(root, paths)``
and ``verify_manifest(root, manifest_path)`` each change or check *root* in place.

Implementation Approach
-----------------------
Moves are renames inside one volume, done through a uniquely named holding folder when a
wrapper and its children could share a name. Glob matches are confined by real path before
anything is copied. Manifest verification reuses ``gupkg.integrity.read_manifest`` so gupkg's
own release check and the general service parse the same file the same way.
"""

from __future__ import annotations

import glob
import hashlib
import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path

from ...integrity import ManifestError, read_manifest
from ...model.manifest import ExtractRule
from ..safepath import UnsafePath, check_member_name, confine, is_reparse_point, remove_tree
from .extract import UnpackError

_MAX_STRIP = 32
_MAX_GLOB_MATCHES = 100_000


def strip_levels(root: Path, levels: int | str) -> None:
    """Remove wrapper folders: *levels* of them, or as many as there are with ``"auto"``.

    A wrapper level is a root that holds exactly one entry, a real folder. ``"auto"``
    strips while that is true (at most 32 times); a number must find that many.

    Raises
    ------
    UnpackError
        If a numbered strip finds fewer wrapper folders than asked for.
    """
    wanted = _MAX_STRIP if levels == "auto" else int(levels)
    for done in range(wanted):
        entries = list(root.iterdir())
        if len(entries) != 1 or not entries[0].is_dir() or is_reparse_point(entries[0]):
            if levels == "auto":
                return
            raise UnpackError(f"strip = {levels} needs {levels} wrapper folder(s) but found {done}")
        _hoist(root, entries[0])


def keep_subdir(root: Path, subdir: str) -> None:
    """Make the folder *subdir* of *root* the new root, dropping everything else.

    Raises
    ------
    UnpackError
        If *subdir* is not a folder inside *root*.
    """
    try:
        target = confine(root, subdir)
    except UnsafePath as error:
        raise UnpackError(f"subdir {subdir!r}: {error}") from error
    if not target.is_dir():
        raise UnpackError(f"subdir {subdir!r} is not a folder of the unpacked artifact")
    _hoist(root, target)


def map_extract(root: Path, rules: Sequence[ExtractRule], scratch: Path) -> None:
    """Rebuild *root* from glob rules: each ``src`` pattern is copied to ``dest``.

    A pattern ending in ``/`` selects the contents of the matched folders, otherwise the
    matched files or folders themselves. ``dest`` is relative to the new root.

    Raises
    ------
    UnpackError
        If a pattern is unsafe, matches nothing, or a match is outside *root*.
    """
    mapped = scratch / f"mapped-{uuid.uuid4().hex[:8]}"
    mapped.mkdir()
    for rule in rules:
        contents = rule.src.endswith("/") or rule.src.endswith("\\")
        pattern = rule.src.rstrip("/\\")
        try:
            check_member_name(pattern.replace("*", "x").replace("?", "x").replace("[", "x").replace("]", "x"))
            target_root = mapped if rule.dest == "" else confine(mapped, rule.dest)
        except UnsafePath as error:
            raise UnpackError(f"extract {rule.src!r}: {error}") from error
        matches = sorted(glob.glob(os.path.join(glob.escape(str(root)), pattern.replace("/", os.sep)), recursive=True))
        if not matches:
            raise UnpackError(f"extract: {rule.src!r} matched nothing in the unpacked artifact")
        if len(matches) > _MAX_GLOB_MATCHES:
            raise UnpackError(f"extract: {rule.src!r} matched too many items")
        target_root.mkdir(parents=True, exist_ok=True)
        for match in matches:
            source = Path(match)
            if is_reparse_point(source) or not _inside(source, root):
                raise UnpackError(f"extract: {rule.src!r} reached outside the unpacked artifact")
            if contents:
                if not source.is_dir():
                    raise UnpackError(f"extract: {rule.src!r} ends in '/' but {source.name!r} is not a folder")
                _copy_unique(source, target_root, rule.src)
            elif source.is_dir():
                _copy_unique(source, target_root / source.name, rule.src)
            else:
                _copy_unique(source, target_root / source.name, rule.src)
    for child in list(root.iterdir()):
        remove_tree(child)
    for child in list(mapped.iterdir()):
        os.replace(child, root / child.name)
    mapped.rmdir()


def merge_into(source: Path, destination: Path, replace: bool) -> None:
    """Move the contents of *source* into *destination*.

    With *replace* the destination is emptied first; otherwise an existing file of the same
    name is an error, so an inner archive can never overwrite what the outer one unpacked.

    Raises
    ------
    UnpackError
        If *replace* is false and a file would be overwritten.
    """
    destination.mkdir(parents=True, exist_ok=True)
    if replace:
        for child in list(destination.iterdir()):
            remove_tree(child)
    for current, folders, names in os.walk(source, followlinks=False):
        relative = Path(current).relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            target = target_dir / name
            if target.exists() or is_reparse_point(target):
                raise UnpackError(f"The nested archive would overwrite {(relative / name).as_posix()}")
            os.replace(Path(current) / name, target)


def require_paths(root: Path, paths: Sequence[str]) -> None:
    """Check that every path (or glob) in *paths* exists in *root*.

    Raises
    ------
    UnpackError
        Listing every path that is missing.
    """
    missing: list[str] = []
    for text in paths:
        try:
            confine(root, text.replace("*", "x").replace("?", "x").replace("[", "x").replace("]", "x"))
        except UnsafePath as error:
            raise UnpackError(f"require {text!r}: {error}") from error
        if any(character in text for character in "*?["):
            found = glob.glob(os.path.join(glob.escape(str(root)), text.replace("/", os.sep)))
            ok = bool(found)
        else:
            ok = confine(root, text).exists()
        if not ok:
            missing.append(text)
    if missing:
        raise UnpackError("The unpacked artifact lacks the required path(s): " + ", ".join(missing))


def verify_manifest(root: Path, manifest_path: str) -> None:
    """Check that *root* holds exactly the files its own manifest lists, with the listed digests.

    The manifest is a text file of ``<sha256>  <relative path>`` lines inside the artifact.
    Every other file must be listed and match; a listed file must exist; there must be no
    extra file. The manifest does not list itself.

    Raises
    ------
    UnpackError
        If the manifest is missing or malformed, or any file is modified, missing or extra.
    """
    try:
        location = confine(root, manifest_path)
    except UnsafePath as error:
        raise UnpackError(f"verify_manifest {manifest_path!r}: {error}") from error
    if not location.is_file():
        raise UnpackError(f"The artifact has no manifest at {manifest_path}")
    try:
        expected = read_manifest(location)
    except ManifestError as error:
        raise UnpackError(str(error)) from error

    # Collect what is really there, by relative path, leaving out the manifest itself.
    actual: dict[str, Path] = {}
    own = os.path.normcase(os.path.normpath(location))
    for current, _folders, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(current) / name
            if os.path.normcase(os.path.normpath(path)) != own:
                actual[path.relative_to(root).as_posix()] = path

    problems = [f"extra: {name}" for name in sorted(set(actual) - set(expected))]
    problems += [f"missing: {name}" for name in sorted(set(expected) - set(actual))]
    for name in sorted(set(expected) & set(actual)):
        if _sha256(actual[name]) != expected[name]:
            problems.append(f"modified: {name}")
    if problems:
        shown = "; ".join(problems[:10]) + (f"; and {len(problems) - 10} more" if len(problems) > 10 else "")
        raise UnpackError(f"The artifact does not match its manifest {manifest_path}: {shown}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hoist(root: Path, inner: Path) -> None:
    """Make the folder *inner* (inside *root*) the contents of *root*, dropping the rest."""
    holding = root.parent / f".hoist-{uuid.uuid4().hex[:8]}"
    os.replace(inner, holding)
    for child in list(root.iterdir()):
        remove_tree(child)
    for child in list(holding.iterdir()):
        os.replace(child, root / child.name)
    holding.rmdir()


def _inside(path: Path, root: Path) -> bool:
    """Return whether the real *path* is inside the real *root*."""
    real, base = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return real == base or real.startswith(base.rstrip("\\/") + os.sep)


def _copy_unique(source: Path, destination: Path, rule: str) -> None:
    """Copy a file or a folder tree to *destination*, refusing to overwrite a file an earlier rule wrote.

    Two rules, or two matches of one rule, that map to the same destination file are an error: the
    result must not depend on the order in which the glob matches came.
    """
    if source.is_file():
        files = [(source, destination)]
    else:
        files = [
            (Path(current) / name, destination / Path(current).relative_to(source) / name)
            for current, _folders, names in os.walk(source, followlinks=False) for name in names
        ]
        destination.mkdir(parents=True, exist_ok=True)
        for current, folders, _names in os.walk(source, followlinks=False):
            for folder in folders:
                (destination / Path(current).relative_to(source) / folder).mkdir(parents=True, exist_ok=True)
    for origin, target in files:
        if os.path.lexists(target):
            raise UnpackError(f"extract: {rule!r} maps {origin.name!r} to a destination that already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)


def _sha256(path: Path) -> str:
    """Return the lower-case SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
