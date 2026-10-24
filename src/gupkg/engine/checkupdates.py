"""Run ``check-updates`` for one package: track, compare, write the version directory, populate it, stop (design 7.4, 13.1).

The tracker (``_check_updates``) is the only code that discovers new versions. This module runs it through the
built-in providers, lets the catalog compare the result with the known versions, renders the pinned ``pkg.toml`` with
``render_pinned`` (strip every template and the ``[track]`` table, fill ``sha256`` only from a checksum the tracker
discovered), writes ``<package>\\<version>\\`` atomically with the support files but without any update-check code and with
the package's own bundled gupkg carried forward where no root forwarder exists (decision D32), and, unless
``--no-download``, downloads, verifies and populates its ``App``. It then stops: ``current``, shortcuts, shims and the
environment are unchanged. A system package checked from a normal shell is only reported (nothing under the system root
is written); a version whose download the checksum rules refuse stays written without ``App`` (decision D46).

Usage and API
-------------
Call ``check_updates_one(engine, request, resolved, catalog, settings)``; it returns a ``PackageResult`` with status
``new-version`` (and the version in ``version``) or ``no-new-version``. A refused checksum is in ``error`` with the
version directory still written; ``details`` says what was written and populated.

Implementation Approach
-----------------------
Whether the root is written is decided first from the mode and the elevation of the run: system packages need an
elevated run, user and portable packages a normal one (the resolution of the mode already refused the other
combinations, decision D31). A report-only run builds its HTTP client without a download cache and its scratch folder
in the system's temporary folder, so it creates nothing below the root. The new version directory is validated like any
other version directory before it is moved into place, and an existing directory is never overwritten.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..catalog.catalog import Catalog, NotInstalled
from ..catalog.local import DATA_DIR_NAME, is_reparse_point
from ..model.checksum import ChecksumError
from ..model.manifest import (
    INSTALLER_LABEL,
    Manifest,
    ManifestError,
    ManifestKind,
    manifest_to_toml,
    parse_manifest,
    render_pinned,
)
from ..model.mode import ModeContext
from ..model.plan import PackageResult
from ..model.settings import Settings
from ..model.state import PackageView
from ..model.version import MANIFEST_NAME, TRACKER_DIR_NAME
from ..modes import ResolvedMode
from ..pkgcheck import check_directory, defines_track
from ..report.events import Step, Warning, emit, phase
from ..services.http import HttpClient, github_environment
from ..services.safepath import remove_tree
from ..services.track import TrackEnvironment, run_tracker
from .appbuild import populate_version
from .context import Engine, require_trusted_tools, require_work_folder, tracker_tools
from .errors import describe
from .executor import carry_forward, emit_skipped, sync_registry_tracker
from .planner import PlanError, plan_check_updates
from .portable import scratch_name
from .receipts import update_receipt
from .stateio import write_track_state



def writes_root(engine: Engine, context: ModeContext) -> bool:
    """Return whether this run may write the package's root: an administrator for system, a normal shell for user and portable.

    A system package checked from a normal shell is only reported (decision D22); the resolution of the mode already
    refused an elevated run on a user or portable root (decision D31).
    """
    return engine.elevated if context.mode == "system" else True


def plan_check(engine: Engine, request, resolved: ResolvedMode, catalog: Catalog):
    """Plan ``check-updates`` for one resolved target without running anything (``--plan``).

    Raises
    ------
    NotInstalled
        If the target has no package root.
    """
    view = resolved.resolved.view if resolved.resolved is not None else None
    if view is None or view.local is None:
        name = view.name if view is not None else request.selector
        raise NotInstalled(f"'{name}' is not a local package, so there is nothing to check; use `gupkg install {name}`")
    sync = catalog.check_updates_plan(view).tracker
    writes = writes_root(engine, resolved.context)
    return plan_check_updates(
        request, view, resolved.context, tracker_action=sync.action, writes=writes, download=writes and not request.no_download
    )


def check_updates_one(
    engine: Engine, request, resolved: ResolvedMode, catalog: Catalog, settings: Settings
) -> PackageResult:
    """Check one package for a newer version and write (and, by default, populate) it without activating anything.

    Parameters
    ----------
    engine : Engine
        The machine, the clock and the HTTP settings.
    request : Request
        The ``check-updates`` request (``no_download``, ``skip``).
    resolved : ResolvedMode
        The resolved target and install mode.
    catalog : Catalog
        Used to compare the tracker's result with the known versions.
    settings : Settings
        The effective settings.

    Returns
    -------
    PackageResult
        ``new-version`` with the version found, or ``no-new-version``; failures are raised.

    Raises
    ------
    NotInstalled
        If the target has no package root (hint: ``install``).
    TrackError, ChecksumDiscoveryError, HookError, HttpError
        If the tracker fails; nothing was written.
    """
    context = resolved.context
    view = resolved.resolved.view if resolved.resolved is not None else None
    if view is None or view.local is None:
        name = view.name if view is not None else request.selector
        raise NotInstalled(f"'{name}' is not a local package, so there is nothing to check; use `gupkg install {name}`")
    plan = catalog.check_updates_plan(view)
    if plan.action == "nothing":
        return PackageResult(view.name, "check-updates", True, "no-new-version", view.installed.version if view.installed else None,
                             None, context.mode, details={"reason": "nothing to check: the package has no tracker"})

    # Who writes: an administrator in an admin shell for the system root, a normal shell for everything else.
    writes = writes_root(engine, context)
    download = writes and not request.no_download
    package_path = view.local.path
    planned = plan_check_updates(request, view, context, tracker_action=plan.tracker.action, writes=writes, download=download)
    for item in planned.phases[0].steps:
        emit(Step(item.text, item.kind))
    if not writes:
        emit(Step("This is a normal shell and the package is a system package: the result is only reported, nothing is written.", "info"))

    # Track: the tracker is the package's own, or the registry's when the package follows it and has none yet.
    with phase("Resolve"):
        if writes and plan.tracker.action in ("copy", "refresh"):
            sync_registry_tracker(engine, view, package_path, plan.tracker)
    tracker_manifest, tracker_dir = _tracker(view, plan.tracker, package_path, writes)
    result = _run_tracker(engine, context, tracker_manifest, tracker_dir, view.name, package_path, writes)
    comparison = catalog.compare(view, result)
    if not comparison.newer:
        emit(Step(f"{view.name}: {comparison.reason}", "info"))
        if writes:
            write_track_state(package_path, {"checked": engine.now().isoformat(), "found": result.version, "outcome": "no-new-version"})
        return PackageResult(view.name, "check-updates", True, "no-new-version", result.version, None, context.mode,
                             details={"found": result.version, "reason": comparison.reason})
    emit(Step(f"{view.name}: new version {result.version} (newest known: {comparison.newest_known or 'none'})", "add"))
    if not writes:
        return PackageResult(view.name, "check-updates", True, "new-version", result.version, None, context.mode,
                             details={"written": False, "populated": False, "reason": "report only"})

    # Write: render the pinned pkg.toml, strip update-check code, validate, move into place atomically.
    pinned = render_pinned(tracker_manifest, result)
    with phase("Write"):
        version_dir = _write_version(engine, context, view, package_path, tracker_dir, pinned, result.version)
        emit(Step(f"wrote {version_dir}", "add"))
    details: dict[str, Any] = {"found": result.version, "written": True, "populated": False, "path": str(version_dir)}

    # Fetch, Verify, Unpack, Prepare: populate unless --no-download; a refused checksum leaves the directory without App (D46).
    error: dict[str, Any] | None = None
    exit_code = 0
    populated = None
    allow_installer = bool(settings["hooks.allow_vendor_installers"])
    if download and pinned.source.installer is not None and not allow_installer:
        emit(Warning(
            f"{view.name} {result.version} {INSTALLER_LABEL}, and hooks.allow_vendor_installers is false: "
            "the version was written without its App."
        ))
        download = False
    if download:
        try:
            populated = populate_version(
                engine, context, package_path, version_dir, pinned, name=view.name, version=result.version,
                skip=request.skip, record_manifest=bool(pinned.record_manifest and settings["install.record_manifest"]),
                allow_installer=allow_installer,
            )
        except ChecksumError as refusal:
            exit_code, error = describe(refusal)
            emit(Warning(f"{view.name} {result.version} was written without its App: {refusal}"))
        details["populated"] = populated is not None
    else:
        for name in ("Fetch", "Verify", "Unpack", "Prepare"):
            emit_skipped(name)
    with phase("Finish"):
        sections: dict[str, Any] = {} if populated is None else dict(populated.sections)
        if populated is not None and populated.checksum is not None:
            sections["checksum"] = populated.checksum.to_data()
        sections["run"] = {
            "operation": "check-updates", "mode": context.mode, "activated": False, "finished": engine.now().isoformat(),
            "gupkg": engine.version, "log": engine.log_path(package_path) or "",
        }
        receipt = update_receipt(package_path, result.version, sections)
        write_track_state(package_path, {
            "checked": engine.now().isoformat(), "found": result.version, "outcome": "new-version",
            "populated": populated is not None, **({"refused": error["code"]} if error else {}),
        })
    return PackageResult(
        view.name, "check-updates", error is None, "new-version", result.version, None, context.mode, exit_code, error,
        str(receipt), engine.log_path(package_path), (), details,
    )


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------


def _tracker(view: PackageView, sync, package_path: Path, writes: bool) -> tuple[Manifest, Path]:
    """Return the manifest and folder of the tracker to run: the local one, or the registry's when none was copied in."""
    local = view.local.tracker if view.local is not None else None
    fresh = sync.action in ("copy", "refresh") and sync.registry_tracker is not None

    # The registry's tracker, either already copied into the package or (report only) run from the registry cache.
    if fresh:
        folder = package_path / TRACKER_DIR_NAME if writes else sync.registry_tracker.path
        text = (folder / MANIFEST_NAME).read_text(encoding="utf-8-sig")
        return parse_manifest(text, ManifestKind.TRACKER), folder
    if local is None:
        raise PlanError(f"'{view.name}' has no tracker to run")
    if not local.valid:
        raise ManifestError([f"the tracker is invalid: {problem}" for problem in local.problems])
    return local.manifest, local.path


def _run_tracker(
    engine: Engine, context: ModeContext, manifest: Manifest, directory: Path, name: str, package_path: Path, writes: bool
):
    """Run the tracker's provider; a report-only run keeps its cache and scratch folder out of the root."""
    # An elevated run starts the regex worker with its own interpreter: judge both before anything is launched.
    require_trusted_tools(engine, *tracker_tools())
    if writes:
        client = engine.http_client(context, manifest.source.headers)
        work = require_work_folder(engine, package_path / DATA_DIR_NAME / "work") / scratch_name("track")
        work.mkdir()
        try:
            return run_tracker(manifest, TrackEnvironment(engine.system, client, work, directory, name))
        finally:
            remove_tree(work)
    client = HttpClient(
        engine.system, headers=dict(manifest.source.headers), env=github_environment(engine.system, engine.env),
        sleep=engine.sleep, **({} if engine.retries is None else {"retries": engine.retries}),
    )
    with tempfile.TemporaryDirectory(prefix="gupkg-track-") as scratch:
        return run_tracker(manifest, TrackEnvironment(engine.system, client, Path(scratch), directory, name))


# ---------------------------------------------------------------------------
# Writing the version directory
# ---------------------------------------------------------------------------


def _write_version(
    engine: Engine, context: ModeContext, view: PackageView, package_path: Path, tracker_dir: Path, pinned: Manifest, version: str
) -> Path:
    """Write ``<package>\\<version>`` from the tracker: support files without update-check code, the pinned ``pkg.toml``.

    The directory is built in the package's work folder, validated like any version directory, and moved into place
    with one rename; an existing directory is never overwritten.

    Raises
    ------
    ManifestError
        If the rendered version directory is not valid; nothing is written.
    FileExistsError
        If a directory of that name appeared meanwhile.
    """
    destination = package_path / version
    work = require_work_folder(engine, package_path / DATA_DIR_NAME / "work") / scratch_name("version")
    staged = work / version
    try:
        staged.mkdir(parents=True)
        _copy_support_files(tracker_dir, staged)
        (staged / MANIFEST_NAME).write_text(manifest_to_toml(pinned), encoding="utf-8")

        # In a root with no forwarder (a bundled install) the package's own gupkg and launchers are carried forward.
        if not (context.root / DATA_DIR_NAME / "gupkg.cmd").is_file():
            carry_forward(view, staged)

        report = check_directory(staged)
        if not report.ok:
            raise ManifestError([f"the new version directory is not valid: {problem}" for problem in report.errors])
        if os.path.lexists(destination):
            raise FileExistsError(f"{destination} already exists")
        os.replace(staged, destination)
    finally:
        remove_tree(work)
    return destination


def _copy_support_files(tracker_dir: Path, staged: Path) -> None:
    """Copy the tracker's support files into *staged*, leaving out ``pkg.toml`` and every module that defines ``track``.

    A ``track`` hook is never copied into a version directory (design 9.3); links are refused.
    """
    for current, folders, files in os.walk(tracker_dir):
        here = Path(current)
        relative = here.relative_to(tracker_dir)
        for name in list(folders):
            if is_reparse_point(here / name):
                raise ManifestError([f"{here / name} is a link; links are not copied into a version directory"])
        for name in files:
            source = here / name
            if relative == Path(".") and name == MANIFEST_NAME:
                continue
            if is_reparse_point(source):
                raise ManifestError([f"{source} is a link; links are not copied into a version directory"])
            if source.suffix.lower() == ".py" and _defines_track(source):
                continue
            target = staged / relative / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _defines_track(path: Path) -> bool:
    """Return whether the Python file defines ``track``; a file that cannot be read or parsed counts as defining it."""
    try:
        return defines_track(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return True
