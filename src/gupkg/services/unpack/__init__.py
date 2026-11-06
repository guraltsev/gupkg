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
from ...system.port import Mode, System
from ..safepath import UnsafePath, confine, remove_tree, safe_file_name
from .detect import detect_kind, require_kind
from .extract import Budget, Limits, UnpackError, check_tree, extract_archive
from .gitsource import checkout_commit
from .transform import keep_subdir, map_extract, merge_into, require_paths, strip_levels, verify_manifest

__all__ = ["Budget", "Limits", "UnpackError", "UnpackPlan", "UnpackReport", "plan_from_source", "unpack_artifact", "unpack_git"]

# The kinds that are extracted with an archive reader (the others are copied as plain files).
_ARCHIVE_KINDS = ("zip", "7z", "tar", "sfx", "nsis", "msi")


@dataclass(frozen=True)
class UnpackPlan:
    """The unpack options of one ``[source]``, with templates already expanded.

    Attributes
    ----------
    kind : str or None
        The archive kind; ``None`` to detect it.
    strip : int or str or None
        How many leading path components to drop; ``None`` or ``0`` drops none.
    subdir : str or None
        The folder inside the archive to take.
    nested : tuple of NestedArchive
        Archives inside the archive to unpack as well.
    extract : tuple of ExtractRule
        Rules that select which entries are extracted.
    require : tuple of str
        Paths that must exist after unpacking.
    verify_manifest : str or None
        A manifest file that lists the expected files.
    """

    kind: str | None = None
    strip: int | str | None = None
    subdir: str | None = None
    nested: tuple[NestedArchive, ...] = ()
    extract: tuple[ExtractRule, ...] = ()
    require: tuple[str, ...] = ()
    verify_manifest: str | None = None


@dataclass(frozen=True)
class UnpackReport:
    """What an unpack produced: its kind, file count, byte count and, for ``git``, the commit.

    Attributes
    ----------
    kind : str
        The kind of the archive.
    files : int
        How many files were written.
    bytes : int
        How many bytes were written.
    commit : str or None
        The commit checked out, for ``git``.
    """

    kind: str
    files: int
    bytes: int
    commit: str | None = None


def plan_from_source(source: Source) -> UnpackPlan:
    """Return the ``UnpackPlan`` described by a pinned version's ``[source]``.

    Parameters
    ----------
    source : Source
        The version's ``[source]`` table, with templates already expanded.

    Returns
    -------
    UnpackPlan
        The kind and the shared unpack options of *source*.
    """
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
    # Refuse an existing destination, then work only inside a private scratch folder beside it.
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
        budget = Budget(limits)
        _fill(system, artifact, raw, kind, filename, seven_zip, budget)

        # Apply the shared options in their documented order.
        for position, inner in enumerate(plan.nested):
            _nested(system, raw, scratch / f"nested-{position}", inner, seven_zip, budget)
        # Reshape the tree, then check the required paths and the manifest.
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


def unpack_git(system: System, mode: Mode, url: str, ref: str | None, commit: str | None, destination: Path, plan: UnpackPlan,
               *, limits: Limits = Limits()) -> UnpackReport:
    """Check out a repository commit into the new folder *destination*, then apply the shared options.

    Parameters
    ----------
    system : System
        The boundary that runs ``git``.
    mode : Mode
        The run's mode; system mode runs only a trusted ``git``.
    url : str
        The repository address.
    ref : str or None
        A full ``refs/...`` name, used when *commit* is not given.
    commit : str or None
        The pinned 40-digit commit id.
    destination : Path
        A folder that must not exist; it appears complete or not at all.
    plan : UnpackPlan
        The shared options (``strip``, ``subdir``, ``extract``, ``require``, ``verify_manifest``).
    limits : Limits
        Bounds on entries and sizes for the final tree check.

    Returns
    -------
    UnpackReport
        Kind ``git``, the size of the result and the resolved commit.

    Raises
    ------
    UnpackError
        If ``git`` fails, the pinned commit is not what the repository serves, or an option fails.
    """
    # Refuse an existing destination, then check out inside a private scratch folder beside it.
    if destination.exists():
        raise UnpackError(f"{destination} already exists")
    scratch = destination.parent / f".unpack-{uuid.uuid4().hex[:10]}"
    scratch.mkdir()
    try:
        raw = scratch / "raw"
        emit(Step(f"Checking out {url}"))
        resolved = checkout_commit(system, mode, url, ref, commit, raw, scratch)

        # Apply the shared options in their documented order (a git checkout has no nested archives).
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
    return UnpackReport("git", files, size, resolved)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _fill(system: System, artifact: Path, raw: Path, kind: str, filename: str | None,
          seven_zip: Path | None, budget: Budget) -> None:
    """Fill the empty folder *raw* from the artifact: extract an archive or copy a plain file.

    Raises
    ------
    UnpackError
        If *kind* is not one gupkg can extract.
    """
    if kind in ("exe", "file"):
        shutil.copyfile(artifact, raw / safe_file_name(filename or artifact.name))
        return
    if kind not in _ARCHIVE_KINDS:
        raise UnpackError(f"{kind!r} is not an unpack kind that gupkg can extract")
    extract_archive(system, artifact, kind, raw, budget, seven_zip)


def _nested(system: System, raw: Path, holding: Path, inner: NestedArchive, seven_zip: Path | None,
            budget: Budget) -> None:
    """Unpack the inner archive *inner.path* of *raw* and merge or substitute it at *inner.dest*.

    Raises
    ------
    UnpackError
        If a path escapes the tree, the archive is missing, or it is not an archive.
    """
    # Confine both paths to the extracted tree before touching anything.
    try:
        archive = confine(raw, inner.path)
        destination = raw if inner.dest == "" else confine(raw, inner.dest)
    except UnsafePath as error:
        raise UnpackError(f"nested {inner.path!r}: {error}") from error
    if not archive.is_file():
        raise UnpackError(f"nested archive {inner.path!r} is not in the unpacked artifact")
    # The inner file must really be an archive; unpack it into its own holding folder.
    kind = detect_kind(system, archive, archive.name, seven_zip)
    if kind not in _ARCHIVE_KINDS:
        raise UnpackError(f"nested {inner.path!r} is not an archive")
    holding.mkdir()
    emit(Step(f"Unpacking nested {inner.path} ({kind})"))
    extract_archive(system, archive, kind, holding, budget, seven_zip)

    # The inner archive is consumed: it is deleted unless the substitution already removed it.
    if archive.exists():
        archive.unlink()
    merge_into(holding, destination, inner.replace)
