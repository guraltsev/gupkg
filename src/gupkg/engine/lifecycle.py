"""The public entry points of the engine: check-updates, install, update, switch, uninstall, cleanup, check and repair.

Each entry point takes explicit targets, resolves every target once (install mode, root, package, registry entry),
runs the targets one after another, continues past a failure and returns
typed results. Nothing here prints or asks: a front end renders the results and the events, supplies a ``confirm``
callable for the questions the design allows (``uninstall --purge``), and may stop the run
between packages (decision D37: the package that is running finishes cleanly, the rest are reported as skipped).
``plan`` resolves and plans without changing anything and returns the data of ``--plan --json``.

Usage and API
-------------
Build an ``Engine`` and call ``run_batch(engine, requests)`` with ``Request`` objects, or one of the named wrappers
(``check_updates``, ``install``, ``update``, ``switch``, ``uninstall``, ``cleanup``, ``check``, ``repair``). The result is
a ``BatchResult``; ``plan(engine, requests)`` returns plans (and failures) instead. ``cleanup_root`` prunes what belongs
to a whole root: logs beyond the retention and registry trees that are not the active one.

Implementation Approach
-----------------------
A target is resolved inside the scope of the run by ``gupkg.targets.resolve_target`` (which applies the admission rules of
an elevated run), the catalog decides which version is chosen (never running a tracker, decision D11), the planner turns
the choice into data, and the executor runs it. Every exception of one target becomes that target's failed result, so a
batch never stops half way because of one package.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from ..catalog.catalog import Candidate, Catalog, CatalogError, NoValidVersion, NoVersionKnown, NotFound, NotInstalled
from ..catalog.local import DATA_DIR_NAME, is_inside
from ..catalog.record import write_install_record
from ..catalog.registry import cache_state, registry_cache_dir
from ..distcopy import COPIED, DIVERGED, CopyResult, DistributionRefused, copy_distribution
from ..model.manifest import (
    APP_MISSING_ERROR,
    NEEDS_NEWER_GUPKG,
    Manifest,
    ManifestKind,
    NeedsNewerGupkg,
    invalid_checksum_problem,
    parse_manifest,
    require_gupkg_version,
)
from ..model.plan import (
    EXIT_INPUT,
    EXIT_INTERNAL,
    BatchResult,
    CopyIn,
    InstallPlan,
    PackageResult,
    Phase,
    PlanStep,
    Request,
    effect_key,
)
from ..model.scope import InputNeeded, Scope, check_supported
from ..model.settings import Settings
from ..model.state import InstallRecord, Link, PackageView, Root
from ..model.version import MANIFEST_NAME, compare_versions
from ..targets import ResolvedTarget, TargetRequest, resolve_target
from ..report.events import Diagnostic, Error, Prompt, Step, Warning, emit, package_scope
from ..report.logfile import prune_logs
from ..services.safepath import remove_tree
from .checkupdates import check_updates_one, plan_check
from .context import Engine
from .effects import desired_effects, effect_context, verify_each
from .errors import failure
from .effects import launchers_find_gupkg
from .executor import (
    ExecutionInput,
    execute_cleanup,
    execute_install,
    execute_uninstall,
    prune_empty_package,
    sync_registry_tracker,
)
from .health import Diagnosis, diagnose
from .locks import PackageBusy, package_lock
from .planner import Chosen, CleanupFacts, CleanupPlan, InvalidVersion, PlanError, plan_cleanup, plan_install, plan_uninstall
from .receipts import list_receipts, read_receipt

# A work entry younger than this is never removed by ``cleanup`` (it may belong to a run that is still going).
WORK_MIN_AGE = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Named entry points
# ---------------------------------------------------------------------------


def check_updates(
    engine: Engine, selectors: Sequence[str], *, no_download: bool = False, skip: str | None = None
) -> BatchResult:
    """Run the trackers of the named packages and write (and by default populate) their new versions; activate nothing."""
    return run_batch(engine, [Request("check-updates", item, no_download=no_download, skip=skip) for item in selectors])


def install(
    engine: Engine, selectors: Sequence[str], *, refresh: bool = False, skip: str | None = None,
) -> BatchResult:
    """Install the named packages (a package that is installed is repaired); a tracker is never run."""
    return run_batch(engine, [
        Request("install", item, refresh=refresh, skip=skip) for item in selectors
    ])


def update(
    engine: Engine, selectors: Sequence[str], *, skip: str | None = None, download_only: bool = False
) -> BatchResult:
    """Activate the newest known version of each installed package; ``download_only`` prepares it and stops."""
    return run_batch(engine, [Request("update", item, skip=skip, download_only=download_only) for item in selectors])


def switch(engine: Engine, selector: str, version: str) -> BatchResult:
    """Activate another known version of an installed package (the explicit way back)."""
    return run_batch(engine, [Request("switch", selector, version=version)])


def uninstall(engine: Engine, selectors: Sequence[str], *, purge: bool = False) -> BatchResult:
    """Remove the integration of the named packages; ``purge`` also deletes the package root and its data."""
    return run_batch(engine, [Request("uninstall", item, purge=purge) for item in selectors])


def cleanup(
    engine: Engine, selectors: Sequence[str], *, keep: int = 1, bundled: bool = False, sweep: bool = False
) -> BatchResult:
    """Remove old ``App`` folders, receipts and work folders of the named packages (never run automatically, D43).

    A named package that another run holds fails with ``package-busy``; with *sweep* (a pass over every package of a root,
    not a list the user wrote) it is left alone and reported as ``locked``.
    """
    return run_batch(engine, [Request("cleanup", item, keep=keep, bundled=bundled, sweep=sweep) for item in selectors])


def check(engine: Engine, selectors: Sequence[str], *, fix: bool = False) -> BatchResult:
    """Report the health of the named packages; ``fix`` re-applies integration and rebuilds a missing ``App``."""
    return run_batch(engine, [Request("check", item, fix=fix) for item in selectors])


def link(engine: Engine, selector: str, entry: str | None = None, *, unlink: bool = False) -> BatchResult:
    """Make a package follow the registry entry *entry* (``registry/name``), its own name's entry again, or none (*unlink*)."""
    return run_batch(engine, [Request("link", selector, link_to=entry, unlink=unlink)])


def repair(engine: Engine, selectors: Sequence[str], *, skip: str | None = None) -> BatchResult:
    """Re-apply the integration and rebuild a missing ``App`` of installed packages."""
    return run_batch(engine, [Request("repair", item, skip=skip) for item in selectors])


# ---------------------------------------------------------------------------
# One call with several targets
# ---------------------------------------------------------------------------


class _Target:
    """One resolved target of a call: its request, the resolved mode and the catalog of its settings."""

    def __init__(self, request: Request, resolved: ResolvedTarget, catalog: Catalog) -> None:
        self.request, self.resolved, self.catalog = request, resolved, catalog


class _UpToDate(Exception):
    """Signal that ``update`` found nothing newer than ``current``."""

    def __init__(self, view: PackageView, mode: str | None) -> None:
        super().__init__(f"{view.name} is up to date")
        self.view, self.mode = view, mode


def run_batch(engine: Engine, requests: Sequence[Request]) -> BatchResult:
    """Run the requests one after another and return one result per request.

    The targets are resolved once. The run continues past a failure.
    When ``engine.cancel()`` becomes true the package that is running finishes, and every later one is reported as
    ``skipped`` and left unchanged.

    Parameters
    ----------
    engine : Engine
        The machine and the callbacks of the run.
    requests : sequence of Request
        The targets and their options.

    Returns
    -------
    BatchResult
        One ``PackageResult`` per request, in order.
    """
    resolved: list[_Target | PackageResult] = [_resolve(engine, request) for request in requests]

    # One package after another; a stop request is honoured between packages (decision D37).
    results: list[PackageResult] = []
    stopped = False
    for request, item in zip(requests, resolved):
        if isinstance(item, PackageResult):
            results.append(item)
            continue
        stopped = stopped or engine.cancel()
        if stopped:
            results.append(PackageResult(request.selector, request.operation, True, "skipped", exit_code=EXIT_INPUT))
            continue
        results.append(_run_one(engine, item, len(requests) == 1))
    return BatchResult(tuple(results))


def plan(engine: Engine, requests: Sequence[Request]) -> list[InstallPlan | PackageResult]:
    """Resolve and plan every request without changing anything; a request that cannot be planned is its failure result."""
    found: list[InstallPlan | PackageResult] = []
    for request in requests:
        target = _resolve(engine, request)
        if isinstance(target, PackageResult):
            found.append(target)
            continue
        try:
            found.append(_plan_one(engine, target))
        except _UpToDate as nothing:
            found.append(PackageResult(nothing.view.name, request.operation, True, "up-to-date", nothing.view.installed.version if nothing.view.installed else None, mode=nothing.mode))
        except Exception as error:  # noqa: BLE001 - a plan that cannot be made is that target's result
            found.append(failure(request.selector, request.operation, error))
    return found


def _resolve(engine: Engine, request: Request) -> _Target | PackageResult:
    """Resolve one request to its mode, root and catalog, or to the failure that stops this target only."""
    try:
        resolved = resolve_target(
            TargetRequest(request.selector, request.operation, engine.no_input),
            scope=engine.scope, system=engine.system, env=engine.env,
        )
        catalog = Catalog(resolved.scope, engine.system)
        target = _Target(request, resolved, catalog)
        _require_supported_mode(target)
        return target
    except Exception as error:  # noqa: BLE001 - every failure of one target is that target's result
        result = failure(request.selector, request.operation, error)
        data = result.error or {}
        emit(Error(f"{request.selector}: {data.get('message', error)}", data.get("hint")))
        return result


def _require_supported_mode(target: _Target) -> None:
    """Refuse a mode the chosen version does not support, before elevation and before any work (design 6.2).

    ``resolve_target`` can judge a path or a named version; for a bare name the version is chosen here. A request
    that cannot choose a version yet (nothing installed, nothing newer) is left to the operation itself.
    """
    request, resolved = target.request, target.resolved
    selection = resolved.resolved
    if request.operation not in ("install", "update", "switch", "repair") or selection is None or resolved.source is not None:
        return
    view = selection.view
    try:
        candidate, _ = _choose(None, target, view, False)
        if candidate.source == "registry":
            manifest = parse_manifest((candidate.path / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
        else:
            state = view.local.version(candidate.version) if view.local is not None else None
            manifest = state.manifest if state is not None else None
    except (CatalogError, PlanError, _UpToDate, OSError, ValueError):
        return
    if manifest is not None:
        check_supported(resolved.scope, manifest.modes, command=request.operation, name=view.name)


def _run_one(engine: Engine, target: _Target, sole: bool) -> PackageResult:
    """Run one resolved target inside its package scope and turn anything it raises into its failed result."""
    request, context = target.request, target.resolved.scope
    view = target.resolved.resolved.view if target.resolved.resolved is not None else None
    if view is not None:
        name = view.name
    elif target.resolved.source is not None:
        name = target.resolved.source.parent.name
    else:
        name = request.selector
    if target.resolved.source is not None:
        # A distribution folder is only the copy's source (design 21.4): logs, lock and receipt belong to the destination.
        path = context.package_dir(name) if _safe_name(name) else context.root
    else:
        path = view.path if view is not None and view.path is not None else context.package_dir(name) if _safe_name(name) else context.root
    with package_scope(name, path, root=str(context.root), sole=sole) as scope:
        try:
            with _held(engine, request, context, name, path):
                result = _dispatch(engine, target)
        except PackageBusy as busy:
            # A sweep over a whole root leaves a busy package alone; a package the user named, like every other operation,
            # fails naming the holder.
            if not (request.operation == "cleanup" and request.sweep):
                result = failure(name, request.operation, busy, mode=context.mode, log=engine.log_path(path))
                emit(Error(str(busy), busy.hint))
            else:
                emit(Warning(f"{name} is in use and was left alone: {busy}"))
                result = PackageResult(
                    name, "cleanup", True, "locked", mode=context.mode, warnings=(str(busy),), details={"locked": True},
                )
        except _UpToDate as nothing:
            emit(Step(f"{nothing.view.name} is up to date", "ok"))
            result = PackageResult(
                name, request.operation, True, "up-to-date", nothing.view.installed.version if nothing.view.installed else None,
                mode=context.mode, log=engine.log_path(path),
            )
        except Exception as error:  # noqa: BLE001 - one package's failure must not stop the batch
            result = failure(name, request.operation, error, mode=context.mode, log=engine.log_path(path))
            data = result.error or {}
            emit(Error(str(data.get("message", error)), data.get("hint")))
            if result.exit_code == EXIT_INTERNAL:
                emit(Diagnostic(traceback.format_exc()))
        scope.result.update({"ok": result.ok, "status": result.status, "version": result.version})
    return result


def _held(engine: Engine, request: Request, context: Scope, name: str, path: Path):
    """Return the lock one operation holds on one package: every operation that changes it, never a read-only check."""
    changes = request.operation != "check" or request.fix
    if not changes or not _safe_name(name) or path == context.root:
        return nullcontext()
    return package_lock(engine.system, path, request.operation, engine.now, scope=context)


def _safe_name(name: str) -> bool:
    """Return whether *name* can name a package folder."""
    return bool(name) and not any(part in ("", ".", "..") for part in name.split("/")) and ":" not in name and "\\" not in name


def _dispatch(engine: Engine, target: _Target) -> PackageResult:
    """Run the operation of the request on its resolved target."""
    operation = target.request.operation
    if operation == "check-updates":
        return check_updates_one(engine, target.request, target.resolved, target.catalog, target.resolved.settings)
    if operation in ("install", "update", "switch", "repair"):
        return _install_like(engine, target)
    if operation == "uninstall":
        return _uninstall(engine, target)
    if operation == "cleanup":
        return _cleanup(engine, target)
    if operation == "link":
        return _link(engine, target)
    return _check(engine, target)


def _plan_one(engine: Engine, target: _Target) -> InstallPlan:
    """Plan one resolved target for ``--plan`` (nothing is changed)."""
    request, context = target.request, target.resolved.scope
    if request.operation in ("install", "update", "switch", "repair"):
        return _prepare_install(engine, target, execute=False).plan
    if request.operation == "check-updates":
        return plan_check(engine, request, target.resolved, target.catalog)
    view = _view(target)
    if request.operation == "uninstall":
        return plan_uninstall(request, view, context)
    extra = {"cleanup": _cleanup_plan(engine, target, view).to_data()} if request.operation == "cleanup" else {}
    if request.operation == "link":
        extra = {"link": {"to": request.link_to, "unlink": request.unlink}}
    return InstallPlan(
        request.operation, view.name, view.path or context.package_dir(view.name), context.root, context.mode, None,
        view.installed.version if view.installed else None, None, None, (),
        (Phase("Resolve", (PlanStep(f"{request.operation} {view.name}"), PlanStep(context.describe()))),),
        (), False, extra,
    )


def _view(target: _Target) -> PackageView:
    """Return the local package view the target resolved to; a target that is not a local package is an error."""
    resolved = target.resolved.resolved
    if resolved is None or resolved.view.local is None:
        name = resolved.view.name if resolved is not None else target.request.selector
        raise NotInstalled(f"'{name}' is not installed or local; use `gupkg install {name}`")
    return resolved.view


# ---------------------------------------------------------------------------
# Install, update, switch, repair
# ---------------------------------------------------------------------------


def _install_like(engine: Engine, target: _Target) -> PackageResult:
    """Plan and run ``install``, ``update``, ``switch`` or ``repair`` for one target."""
    undo: list[Callable[[], None]] = []
    try:
        return execute_install(engine, _prepare_install(engine, target, execute=True, undo=undo))
    except BaseException:
        # A distribution that an elevated run copied before it could plan is removed again when nothing was installed.
        for step in reversed(undo):
            step()
        raise


def _drop_copy(catalog: Catalog, name: str, context: Scope, copied: CopyResult) -> None:
    """Remove a distribution copy that this call made, unless the package turned out to be installed already."""
    if copied.status == COPIED and _describe_destination(catalog, name, context).installed is None:
        remove_tree(copied.destination)
        prune_empty_package(copied.destination.parent)


def _prepare_install(
    engine: Engine, target: _Target, *, execute: bool, undo: list[Callable[[], None]] | None = None
) -> ExecutionInput:
    """Choose the version, build the effects and the plan, and return what the executor needs.

    *undo* receives what has to be done to take back a change made while preparing (an elevated run copies a
    distribution folder before it can plan from it).

    Raises
    ------
    _UpToDate
        If ``update`` found nothing newer than ``current``.
    """
    request, resolved = target.request, target.resolved
    context, catalog = resolved.scope, target.catalog
    selection = resolved.resolved
    registry = _Registry()
    precopied: CopyResult | None = None
    copy_in: CopyIn | None = None
    distribution: Path | None = resolved.source

    if distribution is not None:
        # A distribution folder outside every root (design 21.4): it is copied into the mode's root, and the install
        # proceeds from the copy.
        name, version = distribution.parent.name, distribution.name
        entry = catalog.registry_entry(name)
        published = entry.version(version) if entry is not None else None
        if entry is not None and published is not None:
            registry = _Registry(entry.registry, published.fingerprint, published.path)
        source_view = selection.view if selection is not None else None
        if source_view is not None and source_view.local is not None:
            state = source_view.local.version(version)
            if state is None or not state.valid or state.manifest is None:
                problems = "; ".join(state.problems) if state is not None else "not a version directory"
                raise InvalidVersion(
                    f"{distribution} is not a valid version directory: {problems}", version=version,
                    problems=state.problems if state is not None else (),
                )
            candidate, manifest = Candidate(version, "local", state.path), state.manifest
            copy_in = CopyIn("distribution", distribution, entry.registry if entry else None, published.fingerprint if published else None)
        else:
            # An elevated run reads the folder only while it copies it, so it plans from the admin-only copy.
            if not execute:
                raise PlanError("an elevated run reads a distribution folder only while it copies it into the root")
            precopied = copy_distribution(distribution, context.root, required=resolved.scope)
            if undo is not None:
                undo.append(lambda copied=precopied: _drop_copy(catalog, name, context, copied))
            if precopied.status == DIVERGED:
                raise DistributionRefused(f"{precopied.destination} exists and differs from {distribution}; it was left alone.")
            candidate = Candidate(version, "local", precopied.destination)
            manifest = parse_manifest((precopied.destination / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
            state = None
        view = _describe_destination(catalog, name, context)
        if state is None:
            state = view.local.version(version) if view.local is not None else None
        chosen = Chosen(candidate, manifest, state)
        package_path = context.package_dir(name)
    else:
        view = selection.view
        try:
            candidate, copy_in = _choose(engine, target, view, execute)
        except NoValidVersion as error:
            # Every version is invalid because its pkg.toml is for a newer gupkg: that is exit code 6, not a mistake (21.3).
            if NEEDS_NEWER_GUPKG in str(error):
                raise NeedsNewerGupkg(str(error)) from error
            raise
        if candidate.source == "registry":
            manifest = parse_manifest((candidate.path / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
            registry = _Registry(candidate.registry, candidate.fingerprint, candidate.path)
            state = None
        else:
            state = view.local.version(candidate.version) if view.local is not None else None
            if state is not None and any(NEEDS_NEWER_GUPKG in problem for problem in state.problems):
                raise NeedsNewerGupkg(f"{view.name} {candidate.version} {NEEDS_NEWER_GUPKG}: {'; '.join(state.problems)}")
            if state is None or state.manifest is None:
                problems = "; ".join(state.problems) if state is not None else "there is no such version directory"
                raise InvalidVersion(
                    f"{view.name} {candidate.version} is not a valid version directory: {problems}", version=candidate.version,
                    problems=state.problems if state is not None else (),
                )

            # Every version directory is validated again before it is installed (design 7.4); a missing App is the planner's to judge.
            other = [problem for problem in state.problems if problem != APP_MISSING_ERROR]
            if other:
                raise InvalidVersion(f"{view.name} {candidate.version} is not valid: {'; '.join(other)}", version=candidate.version, problems=other)
            manifest = state.manifest
            published = view.registry.version(candidate.version) if view.registry is not None else None
            if published is not None:
                registry = _Registry(view.registry.registry, published.fingerprint, published.path)
        chosen = Chosen(candidate, manifest, state)
        package_path = view.path if view.path is not None else context.package_dir(view.name)

    # A package for a newer gupkg is refused with exit code 6, before any effect is planned (design 21.3).
    require_gupkg_version(manifest.requires_gupkg, engine.version, f"{view.name} {candidate.version}")
    if execute:
        _ask_about_other_versions(engine, request, view, candidate.version, context.mode)

    # Effects the version declares for this mode, and what a repair found missing on the machine.
    desired, notes = desired_effects(manifest, effect_context(context, package_path, engine.effect_env))
    notes = (*notes, *resolved.warnings)
    drifted: frozenset[tuple[str, ...]] = frozenset()
    repairing = (
        request.operation in ("install", "repair") and view.installed is not None and view.record is not None
        and view.installed.version.casefold() == candidate.version.casefold()
    )
    if repairing and execute:
        rows = verify_each(engine.system, view.record.effects, mode=context.mode)
        drifted = frozenset(effect_key(effect) for effect, drift, _ in rows if drift)
    url_from = "pkg.toml"
    if copy_in is not None:
        url_from = f"registry {copy_in.registry}" if copy_in.origin == "registry" else "distribution folder"
    install_plan = plan_install(
        request, view, context, chosen, desired, notes=notes, copy_in=copy_in, package_path=package_path,
        url_from=url_from, drifted=drifted, allow_installer=bool(resolved.settings["hooks.allow_vendor_installers"]),
    )
    tracker = catalog.tracker_sync(view) if view.registry is not None else None
    return ExecutionInput(
        install_plan, context, view, chosen, request, resolved.settings, tracker, distribution, registry.fingerprint,
        registry.name, registry.path, precopied=precopied,
        warnings=list(resolved.warnings),
    )


class _Registry:
    """What the followed registry publishes for the chosen version: its name, fingerprint and folder."""

    def __init__(self, name: str | None = None, fingerprint: str | None = None, path: Path | None = None) -> None:
        self.name, self.fingerprint, self.path = name, fingerprint, path


def _describe_destination(catalog: Catalog, name: str, context: Scope) -> PackageView:
    """Return the view of package *name* in the mode's root, or an empty view when it is not installed there yet."""
    try:
        return catalog.describe(name)
    except NotFound:
        return PackageView(name, Root(context.root), None)


def _choose(engine: Engine | None, target: _Target, view: PackageView, execute: bool) -> tuple[Candidate, CopyIn | None]:
    """Choose the version an install-like request activates, and the copy-in it needs when it comes from a registry.

    *engine* is needed only when *execute* is true (a tracker-only registry entry is copied in before the refusal).
    """
    request, catalog = target.request, target.catalog
    selection = target.resolved.resolved
    named = selection.version if selection is not None and selection.named_version else None
    if request.operation == "switch":
        # switch: an installed package and an explicit version.
        if view.installed is None:
            raise NotInstalled(f"'{view.name}' is not installed; use `gupkg install {view.name}`")
        if not request.version:
            raise PlanError("switch needs the version to activate")
        candidate = _pick_named(catalog, view, request.version)
    elif request.operation == "update":
        if named:
            candidate = _pick_named(catalog, view, named)
        else:
            newest = catalog.update_candidate(view)
            if newest is None:
                raise _UpToDate(view, target.resolved.scope.mode)
            candidate = newest
    elif request.operation == "repair":
        if view.installed is None:
            raise NotInstalled(f"'{view.name}' is not installed; use `gupkg install {view.name}`")
        candidate = Candidate(view.installed.version, "local", view.installed.path)
    elif named:
        candidate = _pick_named(catalog, view, named)
    else:
        try:
            candidate = catalog.install_candidate(view)
            _warn_about_newer_invalid_versions(view, candidate)
        except NoVersionKnown:
            # A registry entry with only a tracker: copy the tracker, then stop (design 5.3, case R).
            if execute and view.registry is not None and view.registry.tracker is not None and view.local is None:
                sync = catalog.tracker_sync(view)
                if sync.action == "copy":
                    sync_registry_tracker(engine, view, target.resolved.scope.package_dir(view.name), sync)
            raise
    if candidate.source == "registry":
        return candidate, CopyIn("registry", candidate.path, candidate.registry, candidate.fingerprint)
    return candidate, None


def _ask_about_other_versions(engine: Engine, request: Request, view: PackageView, version: str, mode: str) -> None:
    """Ask before ``install`` or ``update`` goes on when the choice has a catch: an older version, or other versions that are unusable.

    Two questions, each answered by ``--yes`` or by its own option: installing *version* when it is older than the active one
    (``--allow-downgrade``), and installing it while other version directories of the package are broken or not supported by this mode
    or this gupkg (``--allow-other-broken-versions``). ``switch`` (the explicit way back) and a repair of the active version ask nothing.
    """
    if request.operation not in ("install", "update"):
        return
    active = view.installed.version if view.installed is not None else None
    if active is not None and compare_versions(version, active) < 0:
        _confirm(
            engine, f"{view.name} {version} is older than the active version {active}. Install it anyway?",
            allowed=engine.allow_downgrade, flag="--allow-downgrade",
        )
    unusable = []
    for state in view.local.versions if view.local is not None else ():
        if state.name.casefold() == version.casefold():
            continue
        problems = [problem for problem in state.problems if problem != APP_MISSING_ERROR]
        if problems:
            unusable.append(f"{state.name} ({problems[0]})")
        elif state.manifest is not None and not state.manifest.supports(mode):
            unusable.append(f"{state.name} (does not support {mode} mode)")
    if unusable:
        _confirm(
            engine, f"{view.name} has other version directories that are broken or not supported: {'; '.join(unusable)}. Install {version} anyway?",
            allowed=engine.allow_other_broken_versions, flag="--allow-other-broken-versions",
        )


def _warn_about_newer_invalid_versions(view: PackageView, chosen: Candidate) -> None:
    """Say so when a bare install fell back to *chosen* because a newer version directory is not valid."""
    if view.installed is not None or view.local is None:
        return
    for state in view.local.versions:
        if not state.valid and state.problems and compare_versions(state.name, chosen.version) > 0:
            reason = "its declared sha256 is malformed" if invalid_checksum_problem(state.problems) else "; ".join(state.problems)
            emit(Warning(f"{view.name} {state.name} is newer than {chosen.version} but not valid ({reason}), so {chosen.version} is installed instead."))


def _pick_named(catalog: Catalog, view: PackageView, version: str) -> Candidate:
    """Return the candidate of ``name@version``; a local version directory that is invalid is refused with its problems."""
    if view.local is not None:
        state = view.local.version(version)
        if state is not None and not state.valid:
            raise InvalidVersion(f"{view.name} {state.name} is not valid: {'; '.join(state.problems)}", version=state.name, problems=state.problems)
    return catalog.pick_version(view, version)


# ---------------------------------------------------------------------------
# Link
# ---------------------------------------------------------------------------


def _link(engine: Engine, target: _Target) -> PackageResult:
    """Record in the install record which registry entry the package follows (decision D2); nothing else changes."""
    request, context = target.request, target.resolved.scope
    view = _view(target)

    # The entry to follow must exist in a configured registry; no entry means the same-name entry again.
    follow: Link | None = None
    if request.unlink:
        follow = Link(unlinked=True)
        status = "unlinked"
    elif request.link_to:
        wanted = target.catalog.resolve(request.link_to)
        if wanted.view.registry is None:
            raise NotFound(f"registry entry '{request.link_to}' was not found")
        follow = Link(registry=wanted.view.registry.registry, name=wanted.view.registry.name)
        status = "linked"
    else:
        status = "following"

    # Only the link changes: the mode and the recorded effects stay as the install wrote them.
    record = view.record or InstallRecord()
    write_install_record(view.path, InstallRecord(record.mode, follow, record.effects))
    where = "no registry entry" if request.unlink else (request.link_to or f"the registry entry named {view.name}")
    emit(Step(f"{view.name} follows {where}", "ok"))
    return PackageResult(
        view.name, "link", True, status, view.installed.version if view.installed else None, None, context.mode,
        log=engine.log_path(view.path), details={"following": request.link_to, "unlinked": request.unlink},
    )


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def _uninstall(engine: Engine, target: _Target) -> PackageResult:
    """Plan and run ``uninstall`` for one target; ``--purge`` is confirmed first (nothing is deleted without it)."""
    request, resolved = target.request, target.resolved
    view = _view(target)
    context = resolved.scope
    uninstall_plan = plan_uninstall(request, view, context)
    if request.purge:
        purge = uninstall_plan.extra["purge"]
        lost = purge["unrecoverable_apps"]
        question = f"Delete {purge['package_root']} including its data folder {purge['data']}?"
        if lost:
            question += f" The App of {', '.join(lost)} cannot be rebuilt (no download_url or script): it is lost for good."
        _confirm(engine, question)
    chosen = Chosen(Candidate(view.installed.version if view.installed else "", "local", view.path), Manifest(ManifestKind.VERSION))
    run = ExecutionInput(
        uninstall_plan, context, view, chosen, request, resolved.settings,
        warnings=list(resolved.warnings),
    )
    return execute_uninstall(engine, run)


def _confirm(engine: Engine, question: str, *, allowed: bool = False, flag: str | None = None) -> None:
    """Ask a consequential question (design 10.1, decision D25): ``--yes`` (or the question's own *flag*, when it is *allowed*) answers it,
    ``--no-input`` or no way to ask is exit 5.

    Raises
    ------
    InputNeeded
        If the question was declined or cannot be asked; nothing was changed.
    """
    if engine.yes or allowed:
        return
    if engine.no_input or engine.confirm is None:
        how = f"--yes or {flag}" if flag else "--yes"
        raise InputNeeded(f"{question} Pass {how} to confirm. Nothing was changed.")
    emit(Prompt(question))
    if not engine.confirm(question):
        raise InputNeeded("Declined. Nothing was changed.")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _cleanup(engine: Engine, target: _Target) -> PackageResult:
    """Plan and run ``cleanup`` for one package (never automatically, decision D43)."""
    context = target.resolved.scope
    view = _view(target)
    cleanup_plan = _cleanup_plan(engine, target, view)
    path_value = next((value for name, value in engine.env.items() if name.upper() == "PATH"), None)
    return execute_cleanup(engine, view.path, cleanup_plan, launchers_find_gupkg(context.root, path_value))


def _old_entries(engine: Engine, folder: Path) -> list[str]:
    """Return the names in *folder* last changed more than ``WORK_MIN_AGE`` ago."""
    if not folder.is_dir():
        return []
    now = engine.now().timestamp()
    old = []
    for item in folder.iterdir():
        try:
            if now - item.lstat().st_mtime > WORK_MIN_AGE.total_seconds():
                old.append(item.name)
        except OSError:
            continue
    return old


def _cleanup_plan(engine: Engine, target: _Target, view: PackageView) -> CleanupPlan:
    """Gather what only the disk can say about a package and plan its cleanup.

    Only work entries older than ``WORK_MIN_AGE`` are offered for removal: a run that is still going (or has just
    died) keeps its staging folder.
    """
    path = view.path
    work = path / DATA_DIR_NAME / "work"
    facts = CleanupFacts(
        tuple(list_receipts(path)), tuple(sorted(_old_entries(engine, work))),
        {state.name: _inputs_present(path, state) for state in view.local.versions},
        tuple(state.name for state in view.local.versions if (state.path / "gupkg").exists()),
    )
    return plan_cleanup(target.request, view, facts)


def _inputs_present(package_path: Path, state) -> bool:
    """Return whether every recorded input of a script-populated version still exists (no record counts as gone)."""
    data = read_receipt(package_path, state.name).data or {}
    inputs = data.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return False
    return all(
        isinstance(item, str) and os.path.lexists(state.path / item) and is_inside(state.path / item, state.path)
        for item in inputs
    )


def cleanup_root(engine: Engine, context: Scope, settings: Settings) -> dict[str, list[str]]:
    """Prune what belongs to a whole root: logs beyond ``[logs]`` retention and registry trees that are not the active one.

    Returns
    -------
    dict
        ``{"logs": [...], "registry_trees": [...]}``: the folders that were pruned or removed.
    """
    done: dict[str, list[str]] = {"logs": [], "registry_trees": []}
    keep_runs, keep_days = settings["logs.keep_runs"], settings["logs.keep_days"]

    # The root's own run logs and every package's.
    folders = [context.log_dir]
    if context.root.is_dir():
        folders += [item / DATA_DIR_NAME / "logs" for item in context.root.iterdir() if item.is_dir() and not item.name.startswith(".")]
    for folder in folders:
        if folder.is_dir():
            # The cleanup run already has its own log in the root's folder, and pruning keeps one run less than it
            # is told so that a run that is about to start fits: telling it one more leaves `keep_runs` runs.
            prune_logs(folder, keep_runs + 1, keep_days, engine.now())
            done["logs"].append(str(folder))

    # Registry trees other than the active revision.
    for registry in settings.registries:
        cache = registry_cache_dir(context.root, registry.name)
        trees = cache / "trees"
        if not trees.is_dir():
            continue
        active = cache_state(cache, engine.scope).revision
        for tree in trees.iterdir():
            if tree.name != active:
                remove_tree(tree)
                done["registry_trees"].append(str(tree))
    return done


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


def _check(engine: Engine, target: _Target) -> PackageResult:
    """Diagnose one package; with ``--fix`` run the repair plan for what can be repaired, then diagnose again."""
    request, resolved = target.request, target.resolved
    context, settings = resolved.scope, resolved.settings
    selection = resolved.resolved
    if selection is None:
        raise NotFound(f"{request.selector} was not found")
    view = selection.view
    record_manifest = bool(settings["install.record_manifest"])
    diagnosis = diagnose(engine.system, view, mode=context.mode, record_manifest=record_manifest)
    fixed: list[str] = []
    if request.fix and diagnosis.installed and diagnosis.fixable:
        fixed = [item.message for item in diagnosis.problems if item.fixable]
        outcome = _repair(engine, _Target(replace(request, operation="repair", fix=False), resolved, target.catalog), diagnosis)
        if not outcome.ok:
            return outcome
        view = target.catalog.describe(view.name)
        diagnosis = diagnose(engine.system, view, mode=context.mode, record_manifest=record_manifest)
    return _check_result(view, context, diagnosis, fixed)


def _repair(engine: Engine, target: _Target, diagnosis: Diagnosis) -> PackageResult:
    """Run the install repair plan for ``check --fix``, applying again the effects that the diagnosis found missing."""
    view = target.resolved.resolved.view
    context = target.resolved.scope
    state = view.local.version(view.installed.version)
    if state is None or state.manifest is None:
        raise PlanError(f"{view.name} {view.installed.version} is not a valid version directory, so it cannot be repaired")
    chosen = Chosen(Candidate(view.installed.version, "local", view.installed.path), state.manifest, state)
    desired, notes = desired_effects(state.manifest, effect_context(context, view.path, engine.effect_env))
    repair_plan = plan_install(
        target.request, view, context, chosen, desired, notes=notes, package_path=view.path, drifted=diagnosis.drifted,
        allow_installer=bool(target.resolved.settings["hooks.allow_vendor_installers"]),
    )
    run = ExecutionInput(
        repair_plan, context, view, chosen, target.request, target.resolved.settings,
        warnings=list(target.resolved.warnings),
    )
    return execute_install(engine, run)


def _check_result(view: PackageView, context: Scope, diagnosis: Diagnosis, fixed: Sequence[str]) -> PackageResult:
    """Build the result of ``check``: healthy or not, with every problem and note."""
    healthy = diagnosis.healthy
    details = {
        "installed": diagnosis.installed,
        "problems": [item.to_data() for item in diagnosis.problems],
        "notes": [item.to_data() for item in diagnosis.notes],
        "fixed": list(fixed),
    }
    for item in diagnosis.problems:
        emit(Warning(item.message))
    for item in diagnosis.notes:
        emit(Step(item.message, "info"))
    status = "healthy" if healthy else "unhealthy"
    if not diagnosis.installed and healthy:
        status = "not-installed"
    return PackageResult(
        view.name, "check", healthy, status, view.installed.version if view.installed else None, None, context.mode,
        0 if healthy else 3,
        None if healthy else {"code": "unhealthy", "message": "; ".join(item.message for item in diagnosis.problems)},
        None, None, (), details,
    )
