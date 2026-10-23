"""Unpack a downloaded artifact into a new ``App`` tree, safely and all at once.

``unpack_artifact`` turns a file into a folder: it works out the kind (``zip``, ``7z``,
``tar``, ``sfx``, ``nsis``, ``msi``, ``exe`` or ``file``) from the file's signature
unless the manifest declares it, extracts into a private scratch folder, applies the
options that every kind shares, and only then moves the finished tree to the destination.
``unpack_git`` does the same for a ``git`` source. There is no ``inno`` kind (decision D39):
a manifest that asks for one fails validation.

Usage and API
-------------
Build an ``UnpackPlan`` from the version's ``[source]`` with ``plan_from_source(source)``
and call ``unpack_artifact(system, artifact, destination, plan)``; the destination must
not exist and its parent must. The options applied, in this order, are ``nested`` (inner
archives, addressed in the raw extraction), ``strip``, ``subdir``, ``extract``, ``require``
and ``verify_manifest``. The result is an ``UnpackReport``. Any failure raises
``UnpackError`` and leaves no destination and no scratch folder behind.

Implementation Approach
-----------------------
Extraction never writes to the destination. Untrusted names, link entries and sizes are
judged before extraction and the tree is walked again after it (``extract``), every path
option is confined to the tree (``transform``), and the scratch folder is removed in a
``finally`` block whatever happens. The destination appears by one rename, so a reader
sees either nothing or a complete, verified tree.
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ...model.manifest import ExtractRule, NestedArchive, Source
from ...report.events import Step, emit
from ...system.port import System
from ..safepath import UnsafePath, confine, remove_tree, safe_file_name
from .detect import detect_kind, require_kind
from .extract import Limits, UnpackError, check_tree, extract_archive
from .gitsource import checkout_commit
from .transform import keep_subdir, map_extract, merge_into, require_paths, strip_levels, verify_manifest

__all__ = ["Limits", "UnpackError", "UnpackPlan", "UnpackReport", "plan_from_source", "unpack_artifact", "unpack_git"]

_ARCHIVE_KINDS = ("zip", "7z", "tar", "sfx", "nsis", "msi")


@dataclass(frozen=True)
class UnpackPlan:
    """The unpack options of one ``[source]``, with templates already expanded."""

    kind: str | None = None
    strip: int | str | None = None
    subdir: str | None = None
    nested: tuple[NestedArchive, ...] = ()
    extract: tuple[ExtractRule, ...] = ()
    require: tuple[str, ...] = ()
    verify_manifest: str | None = None


@dataclass(frozen=True)
class UnpackReport:
    """What an unpack produced: its kind, file count, byte count and, for ``git``, the commit."""

    kind: str
    files: int
    bytes: int
    commit: str | None = None


def plan_from_source(source: Source) -> UnpackPlan:
    """Return the ``UnpackPlan`` described by a pinned version's ``[source]``."""
    return UnpackPlan(
        source.kind, source.strip, source.subdir, source.nested, source.extract, source.require,
        source.verify_manifest,
    )


def unpack_artifact(
    system: System,
    artifact: Path,
    destination: Path,
    plan: UnpackPlan,
    *,
    filename: str | None = None,
    seven_zip: Path | None = None,
    limits: Limits = Limits(),
) -> UnpackReport:
    """Unpack *artifact* into the new folder *destination* according to *plan*.

    Parameters
    ----------
    system : System
        The boundary that runs 7-Zip.
    artifact : Path
        The downloaded file.
    destination : Path
        A folder that must not exist; its parent must. It appears complete or not at all.
    plan : UnpackPlan
        The kind and shared options.
    filename : str, optional
        The published name of the artifact, used for ``exe`` and ``file`` copies and
        for detection when the cache stores it under another name.
    seven_zip : Path, optional
        The 7-Zip executable; the bundled one by default.
    limits : Limits
        Bounds on entries and sizes.

    Returns
    -------
    UnpackReport
        The kind used and the size of the result.

    Raises
    ------
    UnpackError
        If the archive or an option is unsafe, a check fails, or the kind is wrong.
    """
    if destination.exists():
        raise UnpackError(f"{destination} already exists")
    scratch = destination.parent / f".unpack-{uuid.uuid4().hex[:10]}"
    scratch.mkdir()
    try:
        raw = scratch / "raw"
        raw.mkdir()

        # Settle the kind: a declared kind is checked against the file, otherwise the
        # signature (and, for executables, 7-Zip's reading) decides.
        if plan.kind is not None:
            require_kind(plan.kind, artifact)
        kind = plan.kind or detect_kind(system, artifact, filename, seven_zip)
        emit(Step(f"Unpacking {filename or artifact.name} ({kind})"))
        _fill(system, artifact, raw, kind, filename, seven_zip, limits)

        # Apply the shared options in their documented order.
        for position, inner in enumerate(plan.nested):
            _nested(system, raw, scratch / f"nested-{position}", inner, seven_zip, limits)
        if plan.strip not in (None, 0):
            strip_levels(raw, plan.strip)
        if plan.subdir:
            keep_subdir(raw, plan.subdir)
        if plan.extract:
            map_extract(raw, plan.extract, scratch)
        if plan.require:
            require_paths(raw, plan.require)
        if plan.verify_manifest:
            verify_manifest(raw, plan.verify_manifest)

        # One last walk, then the tree appears under its final name in one rename.
        files, size = check_tree(raw, limits)
        os.replace(raw, destination)
    finally:
        remove_tree(scratch)
    return UnpackReport(kind, files, size)


def unpack_git(system: System, url: str, ref: str | None, commit: str | None, destination: Path, plan: UnpackPlan,
               *, limits: Limits = Limits()) -> UnpackReport:
    """Check out a repository commit into the new folder *destination*, then apply the shared options.

    Raises
    ------
    UnpackError
        If ``git`` fails, the pinned commit is not what the repository serves, or an option fails.
    """
    if destination.exists():
        raise UnpackError(f"{destination} already exists")
    scratch = destination.parent / f".unpack-{uuid.uuid4().hex[:10]}"
    scratch.mkdir()
    try:
        raw = scratch / "raw"
        emit(Step(f"Checking out {url}"))
        resolved = checkout_commit(system, url, ref, commit, raw, scratch)
        if plan.strip not in (None, 0):
            strip_levels(raw, plan.strip)
        if plan.subdir:
            keep_subdir(raw, plan.subdir)
        if plan.extract:
            map_extract(raw, plan.extract, scratch)
        if plan.require:
            require_paths(raw, plan.require)
        if plan.verify_manifest:
            verify_manifest(raw, plan.verify_manifest)
        files, size = check_tree(raw, limits)
        os.replace(raw, destination)
    finally:
        remove_tree(scratch)
    return UnpackReport("git", files, size, resolved)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _fill(system: System, artifact: Path, raw: Path, kind: str, filename: str | None,
          seven_zip: Path | None, limits: Limits) -> None:
    """Fill the empty folder *raw* from the artifact: extract an archive or copy a plain file."""
    if kind in ("exe", "file"):
        shutil.copyfile(artifact, raw / safe_file_name(filename or artifact.name))
        return
    if kind not in _ARCHIVE_KINDS:
        raise UnpackError(f"{kind!r} is not an unpack kind that gupkg can extract")
    extract_archive(system, artifact, kind, raw, limits, seven_zip)


def _nested(system: System, raw: Path, holding: Path, inner: NestedArchive, seven_zip: Path | None,
            limits: Limits) -> None:
    """Unpack the inner archive *inner.path* of *raw* and merge or substitute it at *inner.dest*."""
    try:
        archive = confine(raw, inner.path)
        destination = raw if inner.dest == "" else confine(raw, inner.dest)
    except UnsafePath as error:
        raise UnpackError(f"nested {inner.path!r}: {error}") from error
    if not archive.is_file():
        raise UnpackError(f"nested archive {inner.path!r} is not in the unpacked artifact")
    kind = detect_kind(system, archive, archive.name, seven_zip)
    if kind not in _ARCHIVE_KINDS:
        raise UnpackError(f"nested {inner.path!r} is not an archive")
    holding.mkdir()
    emit(Step(f"Unpacking nested {inner.path} ({kind})"))
    extract_archive(system, archive, kind, holding, limits, seven_zip)

    # The inner archive is consumed: it is deleted unless the substitution already removed it.
    if archive.exists():
        archive.unlink()
    merge_into(holding, destination, inner.replace)
