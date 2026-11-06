"""Run ``check-updates`` for one package: ask the sources, write the version directory, populate it, stop (design 7.4, 13.1).

A package has two sources of new versions: the followed registry, which is authoritative, and upstream, found by the tracker
(``_check_updates``). The setting ``check_updates.sources`` of the root says which are asked, and ``--check-registry`` and
``--check-upstream`` override it; by default only the registry is asked, and the tracker runs when the registry pins nothing for
the package. Each source is compared with the local version directories only. If both offer a version
and they differ, the user chooses, and a run that cannot ask takes the registry's. A registry version is copied in as it is. For
an upstream version this module runs the tracker through the built-in providers and renders the pinned ``pkg.toml`` with
``render_pinned`` (strip every template and the ``[track]`` table, fill ``sha256`` only from a checksum the tracker
discovered), writes ``<package>\\<version>\\`` atomically with the support files but without any update-check code and with
the launchers of its own (``install.cmd`` and ``manage.cmd``, in every root) and the package's own bundled gupkg carried
forward where the root has no gupkg-location (decision D32), and, unless
``--no-download``, downloads, verifies and populates its ``App``. It then stops: ``current``, shortcuts, shims and the
environment are unchanged. A system package checked from a normal shell is only reported (nothing under the system root
is written); a version whose download the checksum rules refuse stays written without ``App`` (decision D46). A run that ends
with no version directory in the package root is a failure, except under ``--check-only``, which reports and writes no version.
When nothing newer is on offer and the newest local version has no ``App`` (a failed, refused or skipped download), the run
populates that version; a run that may not download says what is missing.

Usage and API
-------------
Call ``check_updates_one(engine, request, resolved, catalog, settings)``; it returns a ``PackageResult`` with status
``new-version`` (and the version in ``version``), ``update-available`` (``--check-only``) or ``no-new-version``. A refused checksum is in ``error`` with the
version directory still written; ``details`` says what was written and populated.

Implementation Approach
-----------------------
The run writes the package's root: it has one scope and its own token (decision D49), so there is no report-only
run. The new version directory is validated like any
other version directory before it is moved into place, and an existing directory is never overwritten.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..catalog.catalog import Catalog, NotInstalled, NoVersionKnown
from ..catalog.local import DATA_DIR_NAME, is_reparse_point
from ..launchers import LAUNCHERS, write_launchers
from ..model.checksum import ChecksumError
from ..model.manifest import (
    INSTALLER_LABEL,
    Manifest,
    ManifestError,
    ManifestKind,
    manifest_to_toml,
    parse_manifest,
    render_pinned,
    require_gupkg_version,
)
from ..model.scope import Scope
from ..targets import ResolvedTarget
from ..model.plan import PackageResult
from ..model.settings import Settings
from ..model.state import PackageView
from ..model.version import MANIFEST_NAME, TRACKER_DIR_NAME
from ..pkgcheck import check_directory, defines_track
from ..report.events import Prompt, Step, Warning, emit, phase
from ..services.safepath import remove_tree
from ..services.track import TrackEnvironment, run_tracker
from .appbuild import populate_version
from .context import Engine, require_trusted_tools, require_work_folder, tracker_tools
from .effects import LOCATION_FILE, EffectError, desired_effects, effect_context
from .errors import describe
from .executor import carry_forward, copy_registry_version, emit_skipped, sync_registry_tracker
from .planner import NameConflict, PlanError, check_names, name_owners, plan_check_updates
from .portable import scratch_name
from .receipts import update_receipt
from .stateio import write_track_state


@dataclass(frozen=True)
class CheckSources:
    """Which sources one ``check-updates`` run asks.

    Attributes
    ----------
    registry : bool
        Whether the followed registry is asked.
    upstream : bool
        Whether the tracker is run.
    """

    registry: bool
    upstream: bool


def asked_sources(registry: bool | None, upstream: bool | None, configured: str) -> CheckSources:
    """Return the sources a run asks for: the options where given, else the root's setting.

    Parameters
    ----------
    registry, upstream : bool or None
        ``--check-registry`` and ``--check-upstream``; ``None`` when not given.
    configured : str
        The setting ``check_updates.sources``: ``registry``, ``upstream`` or ``both``.

    Returns
    -------
    CheckSources
        What was asked for, before looking at any package. An option that switches one source off leaves the other on, so
        only both options together ask for nothing, and the command line refuses that.

    Examples
    --------
    >>> asked_sources(None, None, "registry")
    CheckSources(registry=True, upstream=False)
    >>> asked_sources(None, False, "upstream")
    CheckSources(registry=True, upstream=False)
    >>> asked_sources(False, None, "both")
    CheckSources(registry=False, upstream=True)
    """
    wants_registry = registry if registry is not None else configured != "upstream" or upstream is False
    wants_upstream = upstream if upstream is not None else configured != "registry" or not wants_registry
    return CheckSources(wants_registry, wants_upstream)


def check_sources(request, view: PackageView, tracked: bool, configured: str) -> CheckSources:
    """Return the sources of a run for one package: those asked for, as far as the package has them.

    Parameters
    ----------
    request : Request
        The ``check-updates`` request (``check_registry``, ``check_upstream``).
    view : PackageView
        The package.
    tracked : bool
        Whether the package has a tracker to run.
    configured : str
        The setting ``check_updates.sources`` of the root.

    Returns
    -------
    CheckSources
        The sources that are on. A package with no pinned registry version, like gupkg itself, falls back to its tracker
        unless ``--check-upstream=false`` was given, and a package with no tracker falls back to the registry unless
        ``--check-registry=false`` was given.
    """
    asked = asked_sources(request.check_registry, request.check_upstream, configured)
    pinned = view.registry is not None and bool(view.registry.versions)
    registry = pinned and (asked.registry or (request.check_registry is None and not tracked))
    wanted = request.check_upstream if request.check_upstream is not None else asked.upstream or not registry
    return CheckSources(registry, bool(wanted) and tracked)


def plan_check(engine: Engine, request, resolved: ResolvedTarget, catalog: Catalog):
    """Plan ``check-updates`` for one resolved target without running anything (``--plan``).

    Parameters
    ----------
    engine : Engine
        The machine and settings (unused by planning, kept for a uniform entrypoint signature).
    request : Request
        The ``check-updates`` request (``no_download``, ``selector``).
    resolved : ResolvedTarget
        The resolved target and install mode.
    catalog : Catalog
        Used to decide what the tracker needs.

    Returns
    -------
    Plan
        The planned phases and steps of the run.

    Raises
    ------
    NotInstalled
        If the target has no package root.
    """
    view = resolved.resolved.view if resolved.resolved is not None else None
    if view is None or view.local is None:
        name = view.name if view is not None else request.selector
        raise NotInstalled(f"'{name}' is not a local package, so there is nothing to check; use `gupkg install {name}`")
    checking = catalog.check_updates_plan(view)
    sources = check_sources(request, view, checking.action == "run", resolved.settings["check_updates.sources"])
    unfinished = catalog.unfinished_version(view)
    return plan_check_updates(
        request, view, resolved.scope, tracker_action=checking.tracker.action, download=not (request.no_download or request.check_only),
        registry=sources.registry, upstream=sources.upstream, unfinished=unfinished.name if unfinished is not None else None,
    )


def check_updates_one(
    engine: Engine, request, resolved: ResolvedTarget, catalog: Catalog, settings: Settings
) -> PackageResult:
    """Ask the sources of one package for a newer version and write (and, by default, populate) it without activating anything.

    Parameters
    ----------
    engine : Engine
        The machine, the clock and the HTTP settings.
    request : Request
        The ``check-updates`` request (``no_download``, ``skip``).
    resolved : ResolvedTarget
        The resolved target and install mode.
    catalog : Catalog
        Used to compare the tracker's result with the known versions.
    settings : Settings
        The effective settings.

    Returns
    -------
    PackageResult
        ``new-version`` with the version found, ``update-available`` under ``--check-only``, or ``no-new-version``; failures
        are raised.

    Raises
    ------
    NotInstalled
        If the target has no package root (hint: ``install``).
    NoVersionKnown
        If the run would end with no version directory in the package root (never under ``--check-only``).
    TrackError, ChecksumDiscoveryError, HookError, HttpError
        If the tracker fails; nothing was written.
    """
    # A package without a local root cannot be written to, so it cannot be checked.
    context = resolved.scope
    view = resolved.resolved.view if resolved.resolved is not None else None
    if view is None or view.local is None:
        name = view.name if view is not None else request.selector
        raise NotInstalled(f"'{name}' is not a local package, so there is nothing to check; use `gupkg install {name}`")

    # With no source to ask there is nothing to do, which is only acceptable when the package root holds a version.
    plan = catalog.check_updates_plan(view)
    sources = check_sources(request, view, plan.action == "run", settings["check_updates.sources"])
    if not sources.registry and not sources.upstream:
        reason = "the package has no tracker" if plan.action == "nothing" else "no source is left to ask"
        _refuse_an_empty_root(request, view, reason)
        return PackageResult(view.name, "check-updates", True, "no-new-version", view.installed.version if view.installed else None,
                             None, context.mode, details={"reason": f"nothing to check: {reason}"})

    # Announce the planned steps so the run reports the same plan that --plan shows.
    download = not (request.no_download or request.check_only)
    package_path = view.local.path
    unfinished = catalog.unfinished_version(view)
    planned = plan_check_updates(request, view, context, tracker_action=plan.tracker.action, download=download,
                                 registry=sources.registry, upstream=sources.upstream,
                                 unfinished=unfinished.name if unfinished is not None else None)
    for item in planned.phases[0].steps:
        emit(Step(item.text, item.kind))

    # Track: the tracker is the package's own, or the registry's when the package follows it and has none yet.
    with phase("Resolve"):
        if sources.upstream and plan.tracker.action in ("copy", "refresh"):
            sync_registry_tracker(engine, view, package_path, plan.tracker)
    result = tracker_manifest = tracker_dir = comparison = None
    notes: tuple[str, ...] = ()
    if sources.upstream:
        tracker_manifest, tracker_dir = _tracker(view, plan.tracker, package_path)
        require_gupkg_version(tracker_manifest.requires_gupkg, engine.version, f"the tracker of {view.name}")
        result = _run_tracker(engine, context, tracker_manifest, tracker_dir, view.name, package_path)

        # What the tracker waived (an optional checksum source that held nothing) is shown, returned and kept in the receipt.
        notes = tuple(result.notes)
        for note in notes:
            emit(Warning(f"{view.name}: {note}"))
        comparison = catalog.compare(view, result)

    # What each source offers: something newer than every local version, or nothing.
    offered = catalog.registry_update(view) if sources.registry else None
    upstream = result.version if comparison is not None and comparison.newer else None

    # Nothing on offer records the check and stops, unless the newest version still needs its App: that is finished below.
    finishing = offered is None and upstream is None and unfinished is not None and download
    if offered is None and upstream is None and not finishing:
        reason = comparison.reason if comparison is not None else f"registry {view.registry.registry} has nothing newer"
        _refuse_an_empty_root(request, view, reason)
        found = result.version if result is not None else view.registry.versions[-1].name
        emit(Step(f"{view.name}: {reason}", "info"))
        if unfinished is not None:
            missing = (f"version {unfinished.name} has no App; `gupkg check-updates {view.name}` without "
                       f"{'--check-only' if request.check_only else '--no-download'} downloads it")
            notes = (*notes, missing)
            emit(Warning(f"{view.name}: {missing}"))
        write_track_state(package_path, {"checked": engine.now().isoformat(), "found": found, "outcome": "no-new-version"})
        return PackageResult(view.name, "check-updates", True, "no-new-version", found, None, context.mode,
                             warnings=notes, details={"found": found, "reason": reason, "notes": list(notes)})

    # --check-only stops here: report what is on offer, write no version and ask nothing.
    if request.check_only:
        found = offered.name if offered is not None else upstream
        offers = {"registry": offered.name if offered is not None else None, "upstream": upstream}
        text = ", ".join(f"{source} offers {version}" for source, version in offers.items() if version)
        emit(Step(f"{view.name}: {text}; nothing was written (--check-only)", "info"))
        write_track_state(package_path, {"checked": engine.now().isoformat(), "found": found, "outcome": "update-available"})
        return PackageResult(view.name, "check-updates", True, "update-available", found, None, context.mode,
                             warnings=notes, details={"found": found, "notes": list(notes), **offers})

    # Write: a version in need of its App is already there; a registry version is copied in as it is; an upstream one is
    # rendered from the tracker and validated.
    if finishing:
        source, version, version_dir, pinned = "local", unfinished.name, unfinished.path, unfinished.manifest
        require_gupkg_version(pinned.requires_gupkg, engine.version, f"{view.name} {version}")
        emit(Step(f"{view.name}: nothing newer is on offer, and version {version} has no App: downloading it", "add"))
        emit_skipped("Write")
    elif offered is not None and (upstream is None or _registry_wins(engine, view, offered.name, upstream)):
        source, version = "registry", offered.name
        pinned = parse_manifest((offered.path / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
        require_gupkg_version(pinned.requires_gupkg, engine.version, f"{view.name} {version}")
        emit(Step(f"{view.name}: new version {version} from registry {view.registry.registry}", "add"))
        with phase("Write"):
            version_dir = copy_registry_version(engine, context, package_path, offered.path, version)
            if not (context.root / DATA_DIR_NAME / LOCATION_FILE).is_file():
                carry_forward(view, version_dir)
            emit(Step(f"wrote {version_dir}", "add"))
    else:
        source, version = "upstream", result.version
        emit(Step(f"{view.name}: new version {version} (newest known: {comparison.newest_known or 'none'})", "add"))
        pinned = render_pinned(tracker_manifest, result)
        with phase("Write"):
            version_dir = _write_version(engine, context, view, package_path, tracker_dir, pinned, version)
            emit(Step(f"wrote {version_dir}", "add"))
    details: dict[str, Any] = {
        "found": version, "source": source, "populated": False, "path": str(version_dir), "notes": list(notes),
    }

    # Fetch, Verify, Unpack, Prepare: populate unless --no-download; a refused checksum leaves the directory without App (D46).
    error: dict[str, Any] | None = None
    exit_code = 0
    populated = None

    # A vendor installer the settings forbid is never run: the version stays written without its App.
    allow_installer = bool(settings["hooks.allow_vendor_installers"])
    if download and pinned.source.installer is not None and not allow_installer:
        emit(Warning(
            f"{view.name} {version} {INSTALLER_LABEL}, and hooks.allow_vendor_installers is false: "
            "the version was written without its App."
        ))
        download = False

    # A name another installed package already provides is refused before any download starts.
    refusal = _name_conflict(engine, context, catalog, view, package_path, pinned) if download else None
    if refusal is not None:
        # Like a refused checksum: the version stays written (the package is `outdated`) and its App is not downloaded.
        exit_code, error = describe(refusal)
        emit(Warning(f"{view.name} {version} was written without its App: {refusal}"))
        download = False

    # Populate the new version's App; a refused checksum is recorded as the result's error, not raised.
    if download:
        try:
            populated = populate_version(
                engine, context, package_path, version_dir, pinned, name=view.name, version=version,
                skip=request.skip, record_manifest=bool(pinned.record_manifest and settings["install.record_manifest"]),
                allow_installer=allow_installer,
            )
        except ChecksumError as refusal:
            # The refusal becomes the result's error; the written version directory stays.
            exit_code, error = describe(refusal)
            emit(Warning(f"{view.name} {version} was written without its App: {refusal}"))
        details["populated"] = populated is not None

    # No download was wanted or allowed: show the four download phases as skipped.
    else:
        for name in ("Fetch", "Verify", "Unpack", "Prepare"):
            emit_skipped(name)

    # Record what was built (receipt) and the outcome of this check (track state) for the next run and for `list`.
    with phase("Finish"):
        sections: dict[str, Any] = {} if populated is None else dict(populated.sections)
        if populated is not None and populated.checksum is not None:
            sections["checksum"] = populated.checksum.to_data()
        if populated is not None:
            sections["built"] = {"time": engine.now().isoformat(), "gupkg": engine.version}
        # The record lives in the version directory beside App and describes the App only: a version without an App
        # (--no-download, or a refused download) gets none, so it holds just its manifest and support files.
        receipt = update_receipt(package_path, version, sections) if populated is not None else None
        write_track_state(package_path, {
            "checked": engine.now().isoformat(), "found": version, "outcome": "new-version", "source": source,
            "populated": populated is not None, **({"refused": error["code"]} if error else {}),
        })
    return PackageResult(
        view.name, "check-updates", error is None, "new-version", version, None, context.mode, exit_code, error,
        str(receipt) if receipt is not None else None, engine.log_path(package_path), notes, details,
    )


def _refuse_an_empty_root(request, view: PackageView, reason: str) -> None:
    """Raise ``NoVersionKnown`` when the run would leave the package root without a version directory, unless it only checks."""
    if request.check_only or view.local.versions:
        return
    raise NoVersionKnown(
        view.name,
        f"'{view.name}' has no version directory and none was written: {reason}. "
        f"Try `gupkg check-updates {view.name} --check-upstream`, or `--check-only` to see what is on offer",
    )


def _registry_wins(engine: Engine, view: PackageView, registry: str, upstream: str) -> bool:
    """Return whether the registry's version is the one to write: it is, unless they differ and the user chooses upstream."""
    if registry.casefold() == upstream.casefold():
        return True

    # A run that cannot ask, or that was told not to, takes the registry's version: the registry is authoritative.
    question = f"{view.name}: the registry offers {registry}, upstream offers {upstream}. Use upstream's {upstream} instead?"
    if engine.yes or engine.no_input or engine.confirm is None:
        emit(Step(f"{view.name}: taking {registry} from the registry; upstream offers {upstream}", "info"))
        return True
    emit(Prompt(question))
    return not engine.confirm(question)


def _name_conflict(
    engine: Engine, context: Scope, catalog: Catalog, view: PackageView, package_path: Path, pinned: Manifest
) -> NameConflict | None:
    """Return the refusal when the new version would provide a name that another installed package provides, else ``None``."""
    try:
        desired, _ = desired_effects(pinned, effect_context(context, package_path, engine.effect_env))
        check_names(view.name, desired, name_owners(catalog.installed_effects(), exclude=view.name))
    except NameConflict as conflict:
        return conflict
    except EffectError:
        return None  # a declaration that cannot be expanded is the install's to report
    return None


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------


def _tracker(view: PackageView, sync, package_path: Path) -> tuple[Manifest, Path]:
    """Return the manifest and folder of the tracker to run: the local one, or the registry's when none was copied in."""
    local = view.local.tracker if view.local is not None else None
    fresh = sync.action in ("copy", "refresh") and sync.registry_tracker is not None

    # The registry's tracker, already copied into the package.
    if fresh:
        folder = package_path / TRACKER_DIR_NAME
        text = (folder / MANIFEST_NAME).read_text(encoding="utf-8-sig")
        return parse_manifest(text, ManifestKind.TRACKER), folder
    if local is None:
        raise PlanError(f"'{view.name}' has no tracker to run")
    if not local.valid:
        raise ManifestError([f"the tracker is invalid: {problem}" for problem in local.problems])
    return local.manifest, local.path


def _run_tracker(
    engine: Engine, context: Scope, manifest: Manifest, directory: Path, name: str, package_path: Path
):
    """Run the tracker's provider with the root's download cache and a scratch folder in the package's work folder."""
    # An elevated run starts the regex worker with its own interpreter: judge both before anything is launched.
    require_trusted_tools(engine, *tracker_tools())
    client = engine.http_client(context, manifest.source.headers)
    work = require_work_folder(engine, package_path / DATA_DIR_NAME / "work") / scratch_name("track")
    work.mkdir()
    try:
        return run_tracker(manifest, TrackEnvironment(engine.system, context, client, work, directory, name))
    finally:
        remove_tree(work)


# ---------------------------------------------------------------------------
# Writing the version directory
# ---------------------------------------------------------------------------


def _write_version(
    engine: Engine, context: Scope, view: PackageView, package_path: Path, tracker_dir: Path, pinned: Manifest, version: str
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
        # Stage the support files and the pinned manifest in the work folder, away from the package.
        staged.mkdir(parents=True)
        _copy_support_files(tracker_dir, staged)
        (staged / MANIFEST_NAME).write_text(manifest_to_toml(pinned), encoding="utf-8")

        # Every version directory carries its own launchers, in every root: this gupkg writes them from the one template, so a
        # double-click on the new version's install.cmd installs exactly that version.
        write_launchers(staged)

        # In a root with no gupkg-location (a bundled install) the package's own gupkg is carried forward as well.
        if not (context.root / DATA_DIR_NAME / LOCATION_FILE).is_file():
            carry_forward(view, staged)

        # Validate the staged directory like any version directory, then publish it with one rename.
        report = check_directory(staged, package=package_path.name)
        if not report.ok:
            raise ManifestError([f"the new version directory is not valid: {problem}" for problem in report.errors])
        if os.path.lexists(destination):
            raise FileExistsError(f"{destination} already exists")
        os.replace(staged, destination)
    finally:
        remove_tree(work)
    return destination


def _copy_support_files(tracker_dir: Path, staged: Path) -> None:
    """Copy the tracker's support files into *staged*, leaving out ``pkg.toml``, the tracker's launcher and every module that defines ``track``.

    A ``track`` hook is never copied into a version directory (design 9.3); links are refused. The version directory gets
    launchers of its own from ``write_launchers``, never the tracker's ``check-updates.cmd``.
    """
    # Walk the whole tracker folder, refusing links and skipping what must not reach a version directory.
    for current, folders, files in os.walk(tracker_dir):
        here = Path(current)
        relative = here.relative_to(tracker_dir)
        for name in list(folders):
            if is_reparse_point(here / name):
                raise ManifestError([f"{here / name} is a link; links are not copied into a version directory"])
        for name in files:
            source = here / name

            # The tracker's own manifest and launchers are replaced by freshly written ones.
            if relative == Path(".") and (name == MANIFEST_NAME or name in LAUNCHERS):
                continue
            if is_reparse_point(source):
                raise ManifestError([f"{source} is a link; links are not copied into a version directory"])

            # Update-check code stays behind: a module that defines ``track`` is not copied.
            if source.suffix.lower() == ".py" and _defines_track(source):
                continue

            # Copy the file with its metadata, creating the folder it lives in.
            target = staged / relative / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _defines_track(path: Path) -> bool:
    """Return whether the Python file defines ``track``; a file that cannot be read or parsed counts as defining it."""
    try:
        return defines_track(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return True
