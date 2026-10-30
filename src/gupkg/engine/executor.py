"""Run a plan through ``System``: copy in, populate, activate, integrate, and write the receipt and install record last.

``execute_install`` serves ``install``, ``update``, ``switch`` and repairs. Population is atomic (``App`` is staged
in ``<package>\\.gupkg\\work`` and renamed); ``current`` moves only after population and the portable data hand-over;
integration applies the effects that are new or changed and removes the stale ones, then broadcasts the environment
change once; the receipt and the install record are written last, and the install record always lists exactly the
effects that are on the machine, even when integration failed half way, so a re-run or ``check --fix`` converges. A
failure before Activate leaves no trace outside the version directory and ``.gupkg\\work``. ``execute_uninstall`` and
``execute_cleanup`` do the same for removal (decision D43: nothing here runs automatically).

Usage and API
-------------
The lifecycle entry points build an ``ExecutionInput`` and call ``execute_install(engine, run)``,
``execute_uninstall(engine, run)`` or ``execute_cleanup(engine, view, plan, ...)``; each returns a ``PackageResult``
and raises for failures that the caller turns into one (``gupkg.engine.errors.failure``). ``copy_registry_version`` and
``sync_registry_tracker`` copy a registry's pinned version or tracker into a package root (copy-in rules of design 5.3).

Implementation Approach
-----------------------
Each phase is a short function that emits its events and does one kind of work. Every removal of something gupkg wrote
checks that the path lies inside the folder the mode owns (``gupkg.engine.effects``), and every folder gupkg deletes
is removed without following links. Elevated runs use admin-only scratch folders only.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..catalog.catalog import TrackerSync
from ..catalog.local import DATA_DIR_NAME, canonical_path, is_inside, is_reparse_point, tree_hash
from ..catalog.record import write_install_record
from ..distcopy import COPIED, DIVERGED, CopyResult, DistributionRefused, copy_distribution
from ..model.checksum import ORIGIN_NOT_VERIFIED, REGISTRY_FINGERPRINT, ChecksumMismatch, ChecksumRecord
from ..model.manifest import parse_manifest
from ..model.scope import Scope
from ..model.plan import InstallPlan, PackageResult, Request
from ..model.settings import Settings
from ..model.state import Effect, InstallRecord, Link, PackageView
from ..model.version import MANIFEST_NAME
from ..report.events import Step, Warning, emit, phase, release_package_log
from ..services.safepath import remove_tree
from .appbuild import Populated, app_section, populate_version
from .context import Engine, require_work_folder
from .effects import LOCATION_FILE, EffectError, apply_effect, changes_environment, effect_context, remove_effect
from ..launchers import VERSION_LAUNCHERS
from ..services.fingerprint import FingerprintError, differing_files, file_digests, verify_directory
from .fingerprint import app_files
from .planner import Chosen, CleanupPlan
from .portable import hand_back, hand_over, scratch_name
from .receipts import read_receipt, update_receipt


class FingerprintMismatch(ChecksumMismatch):
    """Report that a version directory is not the one the registry fingerprints (design 9.2, D29); nothing overrides it."""

    code = "registry-fingerprint-mismatch"


@dataclass
class ExecutionInput:
    """Everything the executor needs to run one install-like plan.

    Attributes
    ----------
    plan : InstallPlan
        What to do.
    context : Scope
        The resolved install mode.
    view : PackageView
        The package as it was when it was planned.
    chosen : Chosen
        The version to activate and its manifest.
    request : Request
        The request (``skip``, ``refresh``).
    settings : Settings
        The effective settings (``install.record_manifest``).
    tracker : TrackerSync, optional
        What to do with the followed registry's tracker.
    distribution : Path, optional
        A distribution folder to copy into the root first.
    registry_fingerprint, registry_name, registry_path : optional
        What the followed registry publishes for this version: its fingerprint, its name and the version folder.
    precopied : CopyResult, optional
        The result of a distribution copy that the caller already made (an elevated run copies the folder before it
        can read the manifest it plans from); ``distribution`` names the source folder.
    """

    plan: InstallPlan
    context: Scope
    view: PackageView
    chosen: Chosen
    request: Request
    settings: Settings
    tracker: TrackerSync | None = None
    distribution: Path | None = None
    registry_fingerprint: str | None = None
    registry_name: str | None = None
    registry_path: Path | None = None
    precopied: CopyResult | None = None
    warnings: list[str] = field(default_factory=list)


# The launchers gupkg writes into every new version directory (``check-updates``), from the one template of ``gupkg.launchers``.
WRITTEN_LAUNCHERS = tuple(VERSION_LAUNCHERS)


@dataclass
class _Origin:
    """Where the version directory came from and how far that is vouched for."""

    kind: str = "local"
    copied_from: str | None = None
    source_marked: bool | None = None
    record: ChecksumRecord | None = None
    app_covered: bool | None = None
    """``False`` when a registry fingerprint matched a version that ships its ``App``: the fingerprint ignores that ``App``."""


# ---------------------------------------------------------------------------
# Install, update, switch and repair
# ---------------------------------------------------------------------------


def execute_install(engine: Engine, run: ExecutionInput) -> PackageResult:
    """Run an install-like plan and return its result.

    Parameters
    ----------
    engine : Engine
        The machine boundary, the clock, the HTTP client factory and the log lookup.
    run : ExecutionInput
        The plan with everything it was made from (the view, the chosen version, the settings, the registry's facts).

    Returns
    -------
    PackageResult
        ``installed``, ``repaired``, ``updated``, ``switched`` or ``prepared`` when it succeeded; a failed result with
        the code ``integrate-failed`` when some effects could not be applied (the others are applied and recorded).

    Raises
    ------
    ChecksumError, UnpackError, PrepareError, HookError, HttpError, AppBuildError
        If population fails; nothing outside the version directory and ``.gupkg\\work`` has changed.
    DistributionRefused, FingerprintMismatch
        If a distribution folder is refused or does not match the registry's fingerprint (the copy is removed).
    """
    plan, context, manifest = run.plan, run.context, run.chosen.manifest
    package_path, version = plan.package_path, plan.version
    version_dir = package_path / version

    # Resolve: show what was decided, then copy in whatever the choice needs.
    with phase("Resolve"):
        for step in plan.phases[0].steps:
            emit(Step(step.text, step.kind))
        origin = _copy_in(engine, run)
        if run.tracker is not None and run.tracker.action in ("copy", "refresh"):
            sync_registry_tracker(engine, run.view, package_path, run.tracker)
        if origin.kind == "local":
            origin = _label_local(engine, run, version_dir, origin)

    # Fetch, Verify, Unpack, Prepare: populate App unless it is already there.
    populated: Populated | None = None
    populate = plan.populate
    if populate is not None and populate.needed:
        populated = populate_version(
            engine, context, package_path, version_dir, manifest, name=plan.package, version=version,
            skip=run.request.skip, record_manifest=_records_manifest(manifest, run.settings), replace=run.request.refresh,
            allow_installer=bool(run.settings["hooks.allow_vendor_installers"]),
        )
    else:
        for name in ("Fetch", "Verify", "Unpack", "Prepare"):
            emit_skipped(name)

    # Activate: hand the portable data over, then switch current.
    activated = _activate(engine, run, version_dir)

    # Integrate: diff the effects against the install record; the record lists what is really applied.
    with _after_switch(run, activated, "Integrate"):
        applied, failures, broadcast_warning = _integrate(engine, run)

    # Finish: the receipt and the install record come last.
    with _after_switch(run, activated, "Finish"), phase("Finish") as finishing:
        # A download-only update changes no integration, so the install record is left as it is.
        if not run.request.download_only:
            record = InstallRecord(context.mode, _link_after(run), tuple(applied))
            write_install_record(package_path, record)
        receipt = _write_receipt(engine, run, populated, origin)
        if failures:
            finishing.fail()
    warnings = [*run.warnings, *([broadcast_warning] if broadcast_warning else [])]
    if failures:
        return PackageResult(
            plan.package, plan.operation, False, "failed", version, plan.previous, context.mode, 3,
            {"code": "integrate-failed", "message": "These effects were not applied: " + "; ".join(failures)
             + ". The ones that were applied are recorded; run `gupkg check --fix` or install again."},
            str(receipt), engine.log_path(package_path), tuple(warnings), {"applied": _describe(applied), "failed": failures},
        )
    return PackageResult(
        plan.package, plan.operation, True, _status(plan), version, plan.previous, context.mode, 0, None,
        str(receipt), engine.log_path(package_path), tuple(warnings),
        {"origin": origin.kind, "populated": populated is not None, **({"label": origin.record.decision} if origin.record else {})},
    )


@contextmanager
def _after_switch(run: ExecutionInput, activated: bool, step: str) -> Iterator[None]:
    """Say, when a step that runs after ``current`` moved fails with an ``OSError``, which step failed and where ``current`` points now.

    ``current`` is not moved back: the integration and the install record may already describe the new version, so a rollback
    would have to undo those too. The message states the real state and the command that returns to the old version.
    The exception type stays ``OSError``, so the exit code does not change.
    """
    try:
        yield
    except OSError as error:
        if not activated:
            raise
        plan = run.plan
        old = plan.previous if plan.previous and plan.previous.casefold() != (plan.version or "").casefold() else None
        back = (
            f"; version {old} is kept. `gupkg check {plan.package}` shows its state, `gupkg switch {plan.package} {old}` returns to the old version"
            if old else f". `gupkg check {plan.package}` shows its state"
        )
        raise OSError(
            f"the {step} step failed: {error}. The package is now on {plan.version}: `current` points to "
            f"{plan.version}{back}."
        ) from error


def emit_skipped(name: str) -> None:
    """Report a phase that did not run: a start event and an end event with the status ``skipped``."""
    with phase(name) as skipped:
        skipped.skip()


def _status(plan: InstallPlan) -> str:
    """Return the status word of a successful install-like plan."""
    if plan.extra.get("download_only"):
        return "prepared"
    if plan.operation == "switch":
        return "switched"
    if plan.operation == "update":
        return "updated"
    if plan.previous is not None and plan.previous.casefold() == (plan.version or "").casefold():
        return "repaired"
    return "installed"


def _records_manifest(manifest: Any, settings: Settings) -> bool:
    """Return whether this install records the SHA-256 of every ``App`` file (package opt-out and global setting)."""
    return bool(manifest.record_manifest and settings["install.record_manifest"])


def _describe(effects: Sequence[Effect]) -> list[str]:
    """Return one readable line per effect."""
    lines = []
    for effect in effects:
        fields = effect.fields
        lines.append(f"{effect.kind} {fields.get('path') or fields.get('entry') or fields.get('name')}")
    return lines


# ---------------------------------------------------------------------------
# Copy in
# ---------------------------------------------------------------------------


def _copy_in(engine: Engine, run: ExecutionInput) -> _Origin:
    """Copy the chosen version into the package root (a registry version or a distribution folder), if the plan says so."""
    plan, context = run.plan, run.context
    copy = plan.copy_in
    origin = _Origin()
    if copy is None and run.precopied is None:
        return origin
    version_dir = plan.package_path / (plan.version or "")
    if run.precopied is not None or copy.origin == "distribution":
        source = run.distribution if run.precopied is not None else copy.source
        outcome = run.precopied or copy_distribution(source, context.root, required=context)
        if outcome.status == DIVERGED:
            raise DistributionRefused(
                f"{outcome.destination} exists and differs from {source}; it was left alone. Install the local copy "
                f"with `gupkg install {plan.package}@{plan.version}`, or remove it first."
            )
        origin = _Origin("distribution", str(source), outcome.source_marked)
        emit(Step(f"Copied {outcome.files} files into {outcome.destination}"
                  + (" (the source carried the Mark of the Web; the copies do not)" if outcome.source_marked else ""), "add"))
        created = outcome.status == COPIED
        _require_same_manifest(run, version_dir, created=created)
        _verify_distribution(run, version_dir, origin, created=created)
        return origin
    if copy.origin == "registry":
        created = not os.path.lexists(version_dir)
        copy_registry_version(engine, run.context, plan.package_path, copy.source, plan.version or "")
        origin = _Origin("registry", f"{copy.registry}", None)
        emit(Step(f"Copied {plan.version} from registry {copy.registry}", "add"))
        _require_same_manifest(run, version_dir, created=created)
        _verify_distribution(run, version_dir, origin, created=created)

        # A bundled install (no gupkg-location in the root) keeps its own gupkg: it is carried in after the copy was verified,
        # because the launchers it adds are part of the fingerprint.
        if created and not (context.root / DATA_DIR_NAME / LOCATION_FILE).is_file():
            carry_forward(run.view, version_dir)
    return origin


def _require_same_manifest(run: ExecutionInput, version_dir: Path, *, created: bool) -> None:
    """Require the copied ``pkg.toml`` to be the manifest that was planned; a source that changed while it was copied is refused.

    The plan was made from the source folder; the copy is what gets installed, so it is read again and compared.
    A copy that this run created is removed when it differs.
    """
    try:
        copied = parse_manifest((version_dir / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        copied, problem = None, str(error)
    else:
        problem = "it is not the pkg.toml that was planned"
    if copied != run.chosen.manifest:
        if created:
            remove_tree(version_dir)
            prune_empty_package(run.plan.package_path)
        raise DistributionRefused(f"{version_dir / MANIFEST_NAME} changed while it was being copied ({problem}); nothing was installed.")


def _verify_distribution(run: ExecutionInput, version_dir: Path, origin: _Origin, *, created: bool) -> None:
    """Judge the copied version directory against the registry's fingerprint and set its origin label (design 9.2, D29).

    A match labels it ``registry-fingerprint``; no published fingerprint labels it ``origin-not-verified``; a mismatch
    refuses the install, naming the files that differ, and removes the copy this run created.
    """
    manifest = run.chosen.manifest
    if run.registry_fingerprint is None:
        origin.record = ChecksumRecord(ORIGIN_NOT_VERIFIED)
        return
    problems = verify_directory(version_dir, run.registry_fingerprint.lower(), run.registry_path)
    if not problems:
        origin.record = ChecksumRecord(REGISTRY_FINGERPRINT, registry=run.registry_name)
        emit(Step(f"Verified against registry {run.registry_name}: the fingerprint matches", "ok"))
        _note_uncovered_app(run, origin)
        return
    if created:
        remove_tree(version_dir)
        prune_empty_package(run.plan.package_path)
    raise FingerprintMismatch(
        f"{run.plan.package} {run.plan.version} is not what registry {run.registry_name} publishes: "
        + "; ".join(problems[:21]) + ". Nothing was installed.",
    )


def _note_uncovered_app(run: ExecutionInput, origin: _Origin) -> None:
    """Say, when the version ships its ``App``, that the registry fingerprint (which ignores ``App``) does not vouch for it."""
    if run.chosen.manifest.source.shipped:
        origin.app_covered = False
        run.warnings.append(f"{run.plan.package} {run.plan.version}: verified (App not covered): the shipped App is not covered by the registry fingerprint (it ships inside the version but version 1 of the fingerprint ignores it)")
        emit(Warning(run.warnings[-1]))


def _label_local(engine: Engine, run: ExecutionInput, version_dir: Path, origin: _Origin) -> _Origin:
    """Label a version directory that was already in the root: compare it with the registry's fingerprint when one exists.

    A mismatch here is the user's own edit of a local version, so it is reported, not refused (the local copy wins, design 5.3).
    """
    if run.registry_fingerprint is None:
        return origin
    expected = run.registry_fingerprint.lower()
    if not verify_directory(version_dir, expected) or _differs_only_by_launchers(version_dir, run.registry_path):
        origin.record = ChecksumRecord(REGISTRY_FINGERPRINT, registry=run.registry_name)
        _note_uncovered_app(run, origin)
    else:
        origin.record = ChecksumRecord(ORIGIN_NOT_VERIFIED)
        run.warnings.append(f"{run.plan.package} {run.plan.version} differs from registry {run.registry_name}'s fingerprint")
        emit(Warning(run.warnings[-1]))
    return origin


def _differs_only_by_launchers(version_dir: Path, registry_copy: Path | None) -> bool:
    """Return whether the only difference from the registry's copy is launchers that this version has and the copy lacks.

    ``check-updates`` writes ``install.cmd`` and ``gupkg-tui.cmd`` into every new version directory; the fingerprint covers
    them, so a version written next to a registry copy that has none would otherwise look edited. Compared without them, it is
    the registry's copy. A launcher that the registry copy also has and that differs is a real difference.
    """
    if registry_copy is None:
        return False
    try:
        found = differing_files(file_digests(registry_copy), file_digests(version_dir))
    except FingerprintError:
        return False
    return bool(found) and all(line in {f"{name} (extra)" for name in WRITTEN_LAUNCHERS} for line in found)


def copy_registry_version(engine: Engine, context: Scope, package_path: Path, source: Path, version: str) -> Path:
    """Copy one pinned version of a registry into ``<package>\\<version>``, never overwriting.

    Returns
    -------
    Path
        The version directory. An existing directory is kept as it is (a local directory of the same name wins).

    Raises
    ------
    OSError
        If the copy fails; the staging folder is removed and nothing is left in the package root.
    DistributionRefused
        If the registry's folder contains a link.
    """
    destination = package_path / version
    if os.path.lexists(destination):
        return destination
    package_path.mkdir(parents=True, exist_ok=True)
    work = require_work_folder(engine, package_path / DATA_DIR_NAME / "work") / scratch_name("copy")
    try:
        staged = work / version
        shutil.copytree(source, staged, symlinks=True)
        _refuse_links(staged)
        os.replace(staged, destination)
    finally:
        remove_tree(work)
    return destination


def carry_forward(view: PackageView, target: Path) -> None:
    """Copy the package's own bundled ``gupkg`` folder from its active (else newest) version into *target*.

    A root with no gupkg-location has no installed gupkg to manage the package, so every new version directory of a
    bundled install carries the package's own copy (design 5.3 rule 1, 21.4, decision D32), whichever gupkg runs the
    operation. The launchers are not copied: every gupkg writes them from the template. A folder that *target* already
    has is left alone.
    """
    source: Path | None = None
    if view.installed is not None:
        source = view.installed.path
    elif view.local is not None and view.local.valid_versions:
        source = view.local.valid_versions[-1].path
    if source is None or canonical_path(source) == canonical_path(target):
        return
    item = source / "gupkg"
    if os.path.lexists(item) and not is_reparse_point(item) and item.is_dir() and not os.path.lexists(target / "gupkg"):
        shutil.copytree(item, target / "gupkg", symlinks=True)


def sync_registry_tracker(engine: Engine, view: PackageView, package_path: Path, sync: TrackerSync) -> str | None:
    """Copy (or refresh) the followed registry's tracker into ``<package>\\_check_updates``; return the new content hash.

    ``keep`` and ``none`` do nothing. A refresh replaces the unedited local copy: the old folder is moved aside first and
    restored if the new one cannot be put in place.
    """
    if sync.action not in ("copy", "refresh") or sync.registry_tracker is None:
        return None
    destination = package_path / "_check_updates"
    package_path.mkdir(parents=True, exist_ok=True)
    work = require_work_folder(engine, package_path / DATA_DIR_NAME / "work") / scratch_name("tracker")
    try:
        staged = work / "_check_updates"
        shutil.copytree(sync.registry_tracker.path, staged, symlinks=True)
        _refuse_links(staged)
        old = work / "old"
        if os.path.lexists(destination):
            os.replace(destination, old)
        try:
            os.replace(staged, destination)
        except OSError:
            if old.exists():
                os.replace(old, destination)
            raise
    finally:
        remove_tree(work)
    emit(Step(f"{'Refreshed' if sync.action == 'refresh' else 'Copied'} the registry's tracker into the package", "add"))
    return tree_hash(destination)


def _refuse_links(folder: Path) -> None:
    """Raise ``DistributionRefused`` when a copy contains a link or junction."""
    for current, folders, files in os.walk(folder):
        for name in (*folders, *files):
            if is_reparse_point(Path(current) / name):
                raise DistributionRefused(f"{Path(current) / name} is a link; links are never copied into a package")


def prune_empty_package(package_path: Path) -> None:
    """Remove a package root that this run created and that holds nothing but empty gupkg folders."""
    try:
        data = package_path / DATA_DIR_NAME
        if data.is_dir() and not is_reparse_point(data):
            for item in sorted(data.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                if item.is_dir() and not any(item.iterdir()):
                    item.rmdir()
            if not any(data.iterdir()):
                data.rmdir()
        if not any(package_path.iterdir()):
            package_path.rmdir()
    except OSError:
        return


# ---------------------------------------------------------------------------
# Activate and integrate
# ---------------------------------------------------------------------------


def _activate(engine: Engine, run: ExecutionInput, version_dir: Path) -> bool:
    """Hand the portable data over and switch ``current`` to *version_dir*; return whether ``current`` moved."""
    plan, context = run.plan, run.context
    if next(item for item in plan.phases if item.name == "Activate").skipped:
        emit_skipped("Activate")
        return False
    with phase("Activate"):
        system, package_path = engine.system, plan.package_path
        if run.chosen.manifest.portable is not None:
            _hand_over_data(engine, run, version_dir)
        emit(Step(f"current -> {plan.version}", "add"))
        current = package_path / "current"
        try:
            system.create_junction(current, version_dir)
        except (OSError, RuntimeError) as error:
            # Replacing a junction removes the old one first: put the previous version back so the package stays installed.
            previous = run.view.installed.path if run.view.installed is not None else None
            if previous is not None and not os.path.lexists(current):
                try:
                    system.create_junction(current, previous)
                except (OSError, RuntimeError):
                    pass
            raise OSError(f"current could not be switched to {plan.version}: {error}") from error
    return True


def _hand_over_data(engine: Engine, run: ExecutionInput, version_dir: Path) -> None:
    """Save the outgoing version's persisted state into ``data`` and link it into the incoming ``App`` (design 6.3)."""
    plan = run.plan
    data = plan.package_path / "data"
    if is_reparse_point(data):
        raise DistributionRefused(f"{data} is a link; refusing to keep portable state through it")
    previous = run.view.local.version(plan.previous) if run.view.local and plan.previous else None
    if previous is not None and previous.manifest is not None and previous.manifest.portable and previous.populated:
        saved = hand_back(engine.system, previous.path / "App", data, previous.manifest.portable.persist)
        for item in saved:
            emit(Step(f"saved {item} to data\\", "info"))
    wanted = run.chosen.manifest.portable.persist if run.chosen.manifest.portable else ()
    if wanted:
        data.mkdir(parents=True, exist_ok=True)
        for item in hand_over(engine.system, version_dir / "App", data, wanted):
            emit(Step(f"persisted {item} (data\\)", "info"))


def _integrate(engine: Engine, run: ExecutionInput) -> tuple[list[Effect], list[str], str | None]:
    """Apply the plan's effect diff; return the effects now on the machine, the failures and a broadcast warning.

    New and changed effects are applied first, stale ones removed afterwards. A failure of one effect never
    stops the others, and a failed removal stays in the record so that the next run retries it.
    """
    plan = run.plan
    if not any(item.action != "keep" for item in plan.effects):
        emit_skipped("Integrate")
        return [item.effect for item in plan.effects], [], None
    effect_ctx = effect_context(run.context, plan.package_path, engine.effect_env)
    applied: list[Effect] = []
    failures: list[str] = []
    changed_environment = False
    with phase("Integrate") as integrating:
        for item in (item for item in plan.effects if item.action != "remove"):
            if item.action == "keep":
                applied.append(item.effect)
                continue
            try:
                result = apply_effect(engine.system, item.effect, effect_ctx, item.previous)
            except (OSError, ValueError) as error:
                failures.append(f"{_label(item.effect)}: {error}")
                if item.previous is not None:
                    applied.append(item.previous)
                emit(Warning(failures[-1]))
                continue
            applied.append(result)
            changed_environment = changed_environment or changes_environment(result)
            emit(Step(f"{'+' if item.action == 'add' else '~'} {_label(result)}", "add"))
        for item in (item for item in plan.effects if item.action == "remove"):
            try:
                removed = remove_effect(engine.system, item.effect, effect_ctx)
            except EffectError as error:
                # The record names something outside the mode's folders: it is ignored and dropped, never acted on.
                run.warnings.append(f"Ignored a recorded effect: {error}")
                emit(Warning(run.warnings[-1]))
                continue
            except (OSError, ValueError) as error:
                failures.append(f"{_label(item.effect)}: {error}")
                applied.append(item.effect)
                emit(Warning(failures[-1]))
                continue
            changed_environment = changed_environment or (removed and changes_environment(item.effect))
            emit(Step(f"- {_label(item.effect)}  stale" + (", removed" if removed else ", left in place"), "remove"))

        # One broadcast for the whole change of the environment.
        warning = None
        if changed_environment:
            try:
                engine.system.broadcast_environment_change()
            except OSError as error:
                warning = f"The environment change could not be broadcast: {error}. New programs see it after a sign-in."
                emit(Warning(warning))
        if failures:
            integrating.fail()
    return applied, failures, warning


def _label(effect: Effect) -> str:
    """Return a short name of an effect for events and messages."""
    fields = effect.fields
    name = fields.get("name") or fields.get("entry") or fields.get("path") or ""
    return f"{effect.kind} {str(name).rsplit(chr(92), 1)[-1] if effect.kind in ('shortcut', 'shim', 'file') else name}"


def _link_after(run: ExecutionInput) -> Link | None:
    """Return the link of the install record after this run (the tracker sync updates its hash and revision)."""
    link = run.view.link
    sync = run.tracker
    if sync is None or sync.action not in ("copy", "refresh") or sync.registry_tracker is None:
        return link
    base = link or Link()
    revision = run.view.registry.revision if run.view.registry is not None else base.revision
    return replace(base, revision=revision, tracker_sha256=sync.registry_tracker.sha256)


def _write_receipt(
    engine: Engine, run: ExecutionInput, populated: Populated | None, origin: _Origin
) -> Path:
    """Write the receipt of this version: what it came from, the checksum facts, the ``App`` fingerprint and the log."""
    plan = run.plan
    sections: dict[str, Any] = dict(populated.sections) if populated is not None else {}

    # An App that was already there (shipped, or prepared by an earlier run) is fingerprinted once: the first
    # record is never overwritten, so a later repair cannot bless a file that was changed since.
    app = plan.package_path / (plan.version or "") / "App"
    if populated is None and app.is_dir() and _records_manifest(run.chosen.manifest, run.settings):
        existing = read_receipt(plan.package_path, plan.version or "").data or {}
        if "app" not in existing:
            sections["app"] = app_section(app_files(app), engine.max_recorded_files)
    checksum = populated.checksum if populated is not None and populated.checksum is not None else origin.record
    if checksum is not None:
        sections["checksum"] = checksum.to_data()
    elif populated is not None:
        sections.pop("checksum", None)
    if origin.kind != "local" or origin.source_marked is not None or origin.app_covered is not None:
        sections["origin"] = {
            "kind": origin.kind,
            **({"app_covered": origin.app_covered} if origin.app_covered is not None else {}),
            **({"copied_from": origin.copied_from} if origin.copied_from else {}),
            **({"source_marked": origin.source_marked} if origin.source_marked is not None else {}),
        }
    # When the App was built, and by which gupkg. Only facts about the App: what a run did lives in the run's log.
    if populated is not None or "built" not in (read_receipt(plan.package_path, plan.version or "").data or {}):
        sections["built"] = {"time": engine.now().isoformat(), "gupkg": engine.version}
    return update_receipt(plan.package_path, plan.version or "", sections)


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def execute_uninstall(engine: Engine, run: ExecutionInput) -> PackageResult:
    """Remove every recorded effect, then ``current``; with ``--purge`` delete the package root too.

    A failed removal stays in the install record so that the next run retries it. Version directories (and every
    ``App``, shipped ones above all) stay unless ``--purge`` was given and confirmed by the caller.

    Parameters
    ----------
    engine : Engine
        The machine boundary and the log lookup.
    run : ExecutionInput
        The uninstall plan (``plan_uninstall``) and its package view.

    Returns
    -------
    PackageResult
        ``uninstalled`` or ``purged``; a failed result with the code ``integrate-failed`` when an effect could not be removed.
    """
    plan, context = run.plan, run.context
    package_path = plan.package_path

    effect_ctx = effect_context(context, package_path, engine.effect_env)
    remaining: list[Effect] = []
    failures: list[str] = []
    changed_environment = False

    # Integrate: remove what the install record says was applied.
    if not plan.effects:
        emit_skipped("Integrate")
    else:
        with phase("Integrate") as integrating:
            for item in plan.effects:
                try:
                    removed = remove_effect(engine.system, item.effect, effect_ctx)
                except EffectError as error:
                    # The record names something outside the mode's folders: it is ignored and dropped, never acted on.
                    run.warnings.append(f"Ignored a recorded effect: {error}")
                    emit(Warning(run.warnings[-1]))
                    continue
                except (OSError, ValueError) as error:
                    failures.append(f"{_label(item.effect)}: {error}")
                    remaining.append(item.effect)
                    emit(Warning(failures[-1]))
                    continue
                changed_environment = changed_environment or (removed and changes_environment(item.effect))
                emit(Step(f"- {_label(item.effect)}" + ("" if removed else "  (left in place)"), "remove"))
            if changed_environment:
                try:
                    engine.system.broadcast_environment_change()
                except OSError as error:
                    emit(Warning(f"The environment change could not be broadcast: {error}"))
            if failures:
                integrating.fail()

    # Activate: remove the current junction.
    current = package_path / "current"
    if plan.previous is None and not os.path.lexists(current):
        emit_skipped("Activate")
    else:
        with phase("Activate"):
            if engine.system.is_link(current):
                emit(Step("current removed", "remove"))
                engine.system.remove_link(current)

    # Finish: the record keeps the mode and link; a purge deletes the package root.
    with phase("Finish"):
        if run.request.purge and not failures:
            emit(Step(f"Deleting {package_path} (including data\\)", "remove"))
            release_package_log(package_path)
            try:
                remove_tree(package_path)
            except OSError as error:
                raise OSError(_purge_failure_text(plan.package, package_path, error)) from error
            return PackageResult(
                plan.package, "uninstall", True, "purged", plan.previous, plan.previous, context.mode, 0, None, None,
                engine.log_path(package_path), tuple(run.warnings), {"purged": str(package_path)},
            )
        base = run.view.record or InstallRecord(context.mode, run.view.link)
        write_install_record(package_path, InstallRecord(base.mode or context.mode, base.link, tuple(remaining)))
    if failures:
        return PackageResult(
            plan.package, "uninstall", False, "failed", plan.previous, plan.previous, context.mode, 3,
            {"code": "integrate-failed", "message": "These effects were not removed: " + "; ".join(failures)
             + ". They stay in the install record; run the command again."},
            None, engine.log_path(package_path), tuple(run.warnings), {"failed": failures},
        )
    return PackageResult(
        plan.package, "uninstall", True, "uninstalled", plan.previous, plan.previous, context.mode, 0, None, None,
        engine.log_path(package_path), tuple(run.warnings), {"kept_versions": [item.name for item in run.view.local.versions]},
    )


def _purge_failure_text(name: str, package_path: Path, error: OSError) -> str:
    """Say what a failed ``--purge`` could not delete and why, what is already done, and the command that finishes it.

    By the time the folder is deleted the integration and ``current`` are already removed, so the package is uninstalled; only
    its folder is left. A file that another program holds open (Windows errors 32 and 33) is named as in use.
    """
    where = str(getattr(error, "filename", None) or package_path)
    why = "is in use by another program" if getattr(error, "winerror", None) in (32, 33) else f"could not be deleted ({error.strerror or error})"
    advice = "close the program" if getattr(error, "winerror", None) in (32, 33) else "remove the cause"
    return (
        f"{where} {why}. The package is uninstalled: its integration and `current` are removed; its folder {package_path} is "
        f"still there. To finish, {advice}, then run `gupkg uninstall {name} --purge` again."
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def execute_cleanup(engine: Engine, package_path: Path, plan: CleanupPlan, gupkg_found: bool) -> PackageResult:
    """Remove what a ``CleanupPlan`` names: ``App`` folders, receipts, work entries and (with ``--bundled``) bundled copies.

    Parameters
    ----------
    engine : Engine
        The machine boundary and run settings.
    package_path : Path
        The package root.
    plan : CleanupPlan
        What to remove.
    gupkg_found : bool
        Whether a launcher of this root would find a gupkg through ``gupkg-location`` or ``PATH`` (steps 2 and 3 of the
        launcher order); ``False`` keeps every bundled copy (design 21.8).

    Returns
    -------
    PackageResult
        ``cleaned`` with the lists of what was removed in ``details``.
    """
    removed: dict[str, list[str]] = {"apps": [], "receipts": [], "work": [], "bundled": []}
    for version in plan.drop_apps:
        target = package_path / version / "App"
        if is_inside(target, package_path):
            remove_tree(target)
            removed["apps"].append(version)
            emit(Step(f"removed the App of {version}", "remove"))
    for version in plan.drop_receipts:
        (package_path / DATA_DIR_NAME / "receipts" / f"{version}.toml").unlink(missing_ok=True)
        removed["receipts"].append(version)
        emit(Step(f"removed the receipt of {version}", "remove"))
    work = package_path / DATA_DIR_NAME / "work"
    for name in plan.drop_work:
        entry = work / name
        if is_inside(entry, work) and canonical_path(entry) != canonical_path(work):
            remove_tree(entry)
            removed["work"].append(name)
    if plan.drop_bundled and plan.package.casefold() == "gupkg":
        emit(Step("kept the bundled gupkg of gupkg itself: it is the populate script's input", "info"))
    elif plan.drop_bundled and not gupkg_found:
        emit(Warning(
            "kept the bundled gupkg copies: no launcher of this root would find another gupkg (no usable gupkg-location in "
            "the root's data folder, and no gupkg on PATH); install gupkg into this root first"
        ))
    if gupkg_found and plan.package.casefold() != "gupkg":
        for version in plan.drop_bundled:
            bundled = package_path / version / "gupkg"
            if is_inside(bundled, package_path):
                remove_tree(bundled)
                removed["bundled"].append(version)
                emit(Step(f"removed the bundled gupkg of {version}", "remove"))
    return PackageResult(
        plan.package, "cleanup", True, "cleaned", None, None, None, 0, None, None, engine.log_path(package_path), (),
        {**removed, "kept": [{"version": version, "why": why} for version, why in plan.kept_apps],
         **({"bundled_kept": "no launcher of this root would find another gupkg (gupkg-location or PATH)"} if plan.drop_bundled and not gupkg_found else {})},
    )
