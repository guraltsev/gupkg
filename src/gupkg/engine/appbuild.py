"""Populate the ``App`` of one version atomically: fetch, verify, unpack, prepare, then rename into place (design 4).

``populate_version`` is the built-in download script and the runner of custom populate scripts. It builds
``App`` in a scratch folder below ``<package>\\.gupkg\\work`` and moves the finished folder to
``<version>\\App`` with one rename, so a version directory is never half-populated and a failure leaves nothing
outside the work folder. It applies the checksum policy before a byte is requested (``fetch_artifact``), hashes the
cached file again right before unpacking (``reverify``), runs ``[[prepare]]`` steps, creates the portable markers
or removes the non-portable files, and records the SHA-256 of every file of the finished ``App`` (decision D41).

Usage and API
-------------
Call ``populate_version(engine, context, package_path, version_dir, manifest, name=..., version=..., skip=...)``
with the resolved mode context; it returns a ``Populated`` holding the receipt sections to store. It raises the
exceptions of the services it calls (``ChecksumError``, ``UnpackError``, ``PrepareError``, ``HookError``,
``HttpError``) and ``AppBuildError`` for a failure of its own; the caller turns them into results.

Implementation Approach
-----------------------
The scratch folder is created through ``require_work_folder``, so an elevated run uses it only when it is
admin-only. A shipped ``App`` is never modified, and ``--refresh`` replaces an existing recoverable ``App`` by moving
the old folder aside first and restoring it if the new one cannot be put in place. The only network addresses fetched
are those written in the version's ``pkg.toml`` (the built-in download script) or what a custom script fetches itself
(decision D28). The events of the Fetch, Verify, Unpack and Prepare phases are emitted here, by the code that does
the work.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..catalog.local import DATA_DIR_NAME, is_reparse_point
from ..model.checksum import ChecksumMismatch, ChecksumRecord, git_gate, git_record, unverified_note
from ..model.manifest import INSTALLER_LABEL, Manifest
from ..model.mode import ModeContext
from ..report.events import Step, Warning, emit, phase
from ..services.artifact import fetch_artifact, reverify
from ..services.hooks import HookContext, HookPaths
from ..services.http import HttpClient
from ..services.populate import populate_app, populate_git, prepare_app, run_custom_populate
from ..services.safepath import remove_tree
from ..services.unpack.gitsource import CommitMismatch
from ..redact import redact_url
from ..services.values import file_name_from_url
from .context import MAX_RECORDED_FILES, Engine, require_trusted_tools, require_work_folder
from .fingerprint import aggregate_digest, app_files
from .installer import InstallerError, run_installer
from .portable import apply_mode_adjustments, mode_signature, scratch_name


class AppBuildError(RuntimeError):
    """Report that ``App`` could not be built or put in place (the old ``App``, if any, is restored)."""


@dataclass(frozen=True)
class Populated:
    """What a population produced, as receipt sections.

    ``sections`` holds the keys to merge into the version's receipt (``source``, ``app``, ``inputs``) and
    ``checksum`` the record of a download or a git checkout (``None`` for a script).
    """

    sections: Mapping[str, Any]
    checksum: ChecksumRecord | None = None


def populate_version(
    engine: Engine,
    context: ModeContext,
    package_path: Path,
    version_dir: Path,
    manifest: Manifest,
    *,
    name: str,
    version: str,
    skip: str | None,
    record_manifest: bool,
    replace: bool = False,
    allow_installer: bool = True,
) -> Populated:
    """Build ``<version_dir>\\App`` from the version's ``[source]`` and return the receipt sections.

    Parameters
    ----------
    engine : Engine
        The machine, the HTTP client factory and the limits.
    context : ModeContext
        The resolved install mode (decides the portable markers and the cache folder).
    package_path, version_dir : Path
        The package root and the version directory that holds ``pkg.toml``.
    manifest : Manifest
        The version's manifest (a pinned version, no templates).
    name, version : str
        Identity passed to hooks and shown in events.
    skip : str, optional
        The ``--skip-checksum`` decision of the call.
    record_manifest : bool
        Whether the SHA-256 of every file is recorded (the package's and the global setting together).
    replace : bool
        Replace an ``App`` that exists (``--refresh``).
    allow_installer : bool
        The ``hooks.allow_vendor_installers`` setting; a version that runs a vendor installer is refused when it is false.

    Returns
    -------
    Populated
        The receipt sections of the build.

    Raises
    ------
    ChecksumError, UnpackError, PrepareError, HookError, HttpError
        From the services; the staged ``App`` and scratch folder are removed.
    AppBuildError
        If ``App`` is a link, is missing after the build, or cannot be moved into place.
    InstallerError
        If a vendor installer is refused (the setting, its arguments), fails, times out or leaves an unacceptable ``App``.
    """
    source = manifest.source
    target = version_dir / "App"
    if is_reparse_point(target):
        raise AppBuildError(f"{target} is a link and is not replaced")
    if is_reparse_point(package_path / DATA_DIR_NAME):
        raise AppBuildError(f"{package_path / DATA_DIR_NAME} is a link; refusing to build below it")
    kind = "git" if source.kind == "git" and source.populate == "download" else source.populate
    if kind == "none":
        raise AppBuildError(f"{name} {version} ships its App and has nothing to populate it from")

    # An elevated run unpacks archives with 7-Zip only from an admin-only folder, judged before anything starts.
    if kind in ("download", "git"):
        require_trusted_tools(engine, engine.seven_zip_path())

    # Scratch space lives in the package's own work folder; an elevated run needs it admin-only.
    work_root = require_work_folder(engine, package_path / DATA_DIR_NAME / "work")
    work = work_root / scratch_name("populate")
    work.mkdir()
    staged = work / "App"
    try:
        client = engine.http_client(context, source.headers)
        paths = HookPaths(package_path, version_dir, app=staged, work=work, cache=context.data_dir / "cache")
        sections: dict[str, Any] = {"source": {"kind": kind}}
        checksum: ChecksumRecord | None = None

        # The three ways of filling App: the built-in download script, a git checkout, a custom script.
        if kind == "download":
            checksum, ran = _download_and_unpack(engine, client, manifest, paths, name, version, skip, allow_installer)
            sections["source"]["url"] = redact_url(source.download_url)
            if ran is not None:
                sections["source"]["installer"] = ran
        elif kind == "git":
            checksum = _checkout(engine, client, manifest, paths, name, version, skip)
            sections["source"]["url"] = redact_url(source.download_url)
        else:
            hook = HookContext(name, version, engine.system, client, paths)
            with phase("Unpack"):
                emit(Step(f"Running the populate script {source.script}"))
                staged.mkdir()
                run_custom_populate(hook, source.script)
            sections["source"]["script"] = source.script

        # The App must hold something, whatever filled it.
        if not staged.is_dir() or not any(staged.iterdir()):
            raise AppBuildError(f"the populate step of {name} {version} left an empty App")

        with phase("Prepare"):
            # Mode adjustments touch only an App that gupkg built, never a shipped one.
            if source.shipped:
                emit(Warning(f"The App of {name} {version} is shipped; portable markers and non-portable removals are not applied."))
            else:
                mode_hook = HookContext(name, version, engine.system, client, paths)
                for line in apply_mode_adjustments(staged, manifest, context.mode, mode_hook):
                    emit(Step(line, "info"))

            # Record the SHA-256 of every file (decision D41), unless the package or the settings opt out.
            files = app_files(staged) if record_manifest else None
            emit(Step("The finished App is moved into place", "ok"))
            _move_into_place(staged, target, work_root, replace, engine.replace)
        sections["app"] = app_section(files, engine.max_recorded_files)
        signature = mode_signature(manifest, context.mode)
        if signature is not None and not source.shipped:
            # Which mode the App was prepared for: an install in another mode must not run more steps on top of it (design: App guard).
            sections["app"]["prepared_for"] = {"mode": context.mode, "signature": signature}
        sections["inputs"] = _inputs(version_dir) if kind == "script" else []
        return Populated(sections, checksum)
    finally:
        remove_tree(work)


def _download_and_unpack(
    engine: Engine, client: HttpClient, manifest: Manifest, paths: HookPaths, name: str, version: str, skip: str | None,
    allow_installer: bool,
) -> tuple[ChecksumRecord, list[str] | None]:
    """Fetch the one URL of ``[source]`` under the checksum policy, verify it again, and unpack it into the staged ``App``.

    A version with ``[source.installer]`` runs the verified installer into the staged ``App`` instead of unpacking it
    (``gupkg.engine.installer``); the arguments it ran with are returned for the receipt.
    """
    source = manifest.source

    # An installer is vendor code: it needs a pinned sha256, and the refusal comes before any byte is requested.
    if source.installer is not None and not source.sha256:
        raise InstallerError(
            f"{name} {version} {INSTALLER_LABEL} and pins no sha256; a vendor installer runs only from a download that "
            "matches a pinned sha256 (pin one with `gupkg pkg pin`; --skip-checksum does not apply to it)"
        )

    # The rule runs before any byte is requested; a refusal starts no download.
    with phase("Fetch"):
        fetched = fetch_artifact(
            client, source.download_url, expected_sha256=source.sha256, skip=skip, sha256_from=source.sha256_from,
        )
    with phase("Verify"):
        emit(Step(
            f"SHA-256 {fetched.record.observed_sha256} ({fetched.record.decision})",
            "ok" if fetched.record.verified else "info",
        ))

        # A download that was allowed to go unverified is shown here, where the run reports verification (design 9.1).
        note = unverified_note(fetched.record.decision)
        if note is not None:
            emit(Warning(f"{name} {version} is {note}"))
    with phase("Unpack"):
        # The cached file is hashed again right before it is used (it could have been replaced since).
        reverify(fetched)
        hook = HookContext(
            name, version, engine.system, client,
            HookPaths(paths.package_root, paths.version_root, paths.app, fetched.download.path, paths.work, paths.cache),
        )
        if source.installer is None:
            populate_app(
                engine.system, manifest, fetched.download.path, paths.app, context=hook,
                filename=file_name_from_url(fetched.download.final_url), seven_zip=engine.seven_zip_path(), limits=engine.limits,
            )
            return fetched.record, None
        if not allow_installer:
            raise InstallerError(f"{name} {version} {INSTALLER_LABEL}, and hooks.allow_vendor_installers is false")
        ran = run_installer(engine, fetched, source.installer.run, paths.app, paths.work, source.require, name, version)
        prepare_app(manifest, paths.app, hook)
    return fetched.record, ran


def _checkout(
    engine: Engine, client: HttpClient, manifest: Manifest, paths: HookPaths, name: str, version: str, skip: str | None,
) -> ChecksumRecord:
    """Check out a ``git`` source under the policy (the pinned commit is the anchor) into the staged ``App``."""
    source = manifest.source
    git = source.git

    # The rule runs before git starts: a pinned commit anchors the install, a bare ref needs --skip-checksum.
    with phase("Verify"):
        verification = git_gate(source.download_url, git.commit if git else None, skip)
        if verification.skipped:
            emit(Warning(f"{source.download_url} pins no commit and is not verified: continuing because the call allowed it ({verification.decision})."))
    with phase("Unpack"):
        hook = HookContext(name, version, engine.system, client, paths)
        try:
            report = populate_git(engine.system, manifest, paths.app, context=hook, limits=engine.limits)
        except CommitMismatch as error:
            raise ChecksumMismatch(str(error), source.download_url, expected=error.expected, observed=error.observed) from error
    return git_record(verification, report.commit or "", url=source.download_url)


def _move_into_place(staged: Path, target: Path, work_root: Path, replace: bool, move: Callable[[Path, Path], None]) -> None:
    """Rename the finished *staged* folder to *target*; an old ``App`` is moved aside first and restored on failure."""
    old: Path | None = None
    if os.path.lexists(target):
        if target.is_dir() and not any(target.iterdir()):
            target.rmdir()
        elif replace:
            old = work_root / scratch_name("old-App")
            move(target, old)
        else:
            raise AppBuildError(f"{target} already holds an App; use --refresh to rebuild it")
    try:
        move(staged, target)
    except OSError as error:
        if old is not None:
            try:
                move(old, target)
            except OSError as restore_error:
                # The old App stays where it was moved to (outside the scratch folder that is deleted): nothing is lost.
                raise AppBuildError(
                    f"The App cannot be moved into place at {target}: {error}; the previous App is kept in {old} "
                    f"(it could not be put back: {restore_error})"
                ) from error
        raise AppBuildError(f"The App cannot be moved into place at {target}: {error}") from error
    if old is not None:
        remove_tree(old)


def app_section(files: Mapping[str, str] | None, limit: int = MAX_RECORDED_FILES) -> dict[str, Any]:
    """Return the ``[app]`` receipt section: whether the files are recorded, their count, one digest and the list.

    An ``App`` of more than *limit* (``MAX_RECORDED_FILES``) files records the count and the digest but not the list (the receipt
    stays small enough to be read, see ``MAX_RECEIPT_BYTES``); ``check`` then still sees that something changed, though
    it cannot name the file.
    """
    if files is None:
        return {"recorded": False}
    section: dict[str, Any] = {"recorded": True, "count": len(files), "fingerprint": aggregate_digest(files)}
    if len(files) > limit:
        section["files_omitted"] = True
    else:
        section["files"] = dict(files)
    return section


def _inputs(version_dir: Path) -> list[str]:
    """Return the inputs of a populate script: everything in the version directory except ``App`` and ``.gupkg``.

    The names are the top-level entries, plus every file below ``pkg.local``, so ``cleanup`` can tell that an
    input was removed and keep an ``App`` that could no longer be rebuilt.
    """
    names: list[str] = []
    for entry in sorted(os.scandir(version_dir), key=lambda item: item.name):
        if entry.name.casefold() in ("app", ".gupkg"):
            continue
        names.append(entry.name)
        if entry.name == "pkg.local" and entry.is_dir(follow_symlinks=False):
            for folder, _, files in os.walk(entry.path):
                names.extend(str((Path(folder) / item).relative_to(version_dir)).replace("\\", "/") for item in files)
    return names
