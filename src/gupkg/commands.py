"""Run every gupkg operation and return its :class:`~gupkg.outcome.Outcome`.

This module is the single application layer behind all front ends. The
command line, the package TUI, and the manager TUI only collect input (arguments
or menu selections), call a function here, and display the result with the
formatters in :mod:`gupkg.outcome`; they contain no workflow logic of their own.

Usage and API
-------------
Package operations: build a :class:`PackageRequest` and call
``run_package_command(...)``; ``package_summary(...)`` and
``automatic_scope(...)`` supply the summary and scope a UI shows.

Manager operations: ``load_manager(...)``, ``init_manager(...)``,
``save_manager_config(...)``, ``list_outcome(...)``, then ``plan_updates(...)``,
``execute_updates(...)`` and ``update_outcome(...)`` for bulk updates, plus
``registry_*`` and ``install_registry_package(...)`` for the registry and
``self_outcome(...)`` for gupkg's own installation.

Implementation Approach
-----------------------
Operations take plain values and return outcomes instead of printing or
exiting. Workflows report their progress as events (:mod:`gupkg.report.events`)
to whichever reporters the front end installed, so a front end shows them live
(console, TUI), logs them, or both. Exceptions at the boundary become failed
outcomes carrying the documented exit codes.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import gupkg as package_workflows
from .selfmgmt.elevation import UntrustedLocationError
from .configfix import fix_package_config, has_legacy_metadata
from .configuration import check_metadata_consistency
from .core import (
    EXIT_INTERNAL_ERROR,
    EXIT_MUTATION_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    ActionResult,
    ConfigValidationError,
    PackageIdentity,
    Scope,
    is_version_directory_name,
    read_toml_file,
    write_text_atomic,
)
from .layout import identity_from_path, installed_version
from .legacy_to_gupkg_toml import pick_legacy_metadata_files
from .manager import (
    ManagedTarget,
    ManagerConfig,
    ManagerInventory,
    UpgradePlan,
    default_manager_config,
    discover_manager,
    discover_manager_config,
    execute_upgrade_plan,
    installation_context,
    load_manager_config,
    manager_config_candidates,
    manager_config_text,
    manager_download_target,
    manager_revalidate_target,
    manager_update_target,
    manager_upgrade_target,
    plan_upgrade_all,
)
from .outcome import Outcome, failure
from .report.events import emit_problems, package_scope, phase
from .registry import registry_status, resolve_selector, search_registry, sync_registry
from .system.port import System, is_elevated_run, require_admin_only


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def run_guarded(command: str, operation: Callable[[], ActionResult]) -> Outcome:
    """Run a workflow, mapping exceptions to documented exit codes.

    The workflow reports its progress as events to the installed reporter.
    Its warnings and errors are reported once more here as events, which the
    event stream drops when the workflow already reported them live.

    Parameters
    ----------
    command : str
        Command name reported in the outcome.
    operation : callable
        Workflow returning an :class:`ActionResult`.
    """
    try:
        result = operation()
    except (ConfigValidationError, ValueError, FileNotFoundError) as exc:
        result = ActionResult(ok=False, errors=[str(exc)], exit_code=EXIT_USER_ERROR)
    except OSError as exc:
        result = ActionResult(ok=False, errors=[str(exc)], exit_code=EXIT_MUTATION_ERROR)
    except Exception as exc:  # pragma: no cover - defensive process boundary
        result = ActionResult(
            ok=False, errors=[f"Unexpected internal error: {exc}"], exit_code=EXIT_INTERNAL_ERROR
        )
    emit_problems(result.warnings, result.errors)
    return Outcome(command, result)


def _path_text(value: Path) -> str:
    """Render a path as the normalized absolute form promised in results."""
    return str(Path(value).expanduser().resolve())


# ---------------------------------------------------------------------------
# Package operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageRequest:
    """Describe one package operation exactly as a front end collected it.

    ``command`` is ``install``, ``update``, ``config-check``, or ``config-fix``.
    ``path`` is ``None`` for the current directory.
    """

    command: str
    path: Path | None = None
    scope: Scope = Scope.AUTO
    check_only: bool = False
    download_only: bool = False
    allow_downgrade: bool = False
    refresh_app: bool = False
    no_checksum: bool = False
    shim_linkage: str = "dynamic"
    backup: bool = True
    import_shortcuts: bool = True
    output: Path | None = None

    def command_line(self) -> str:
        """Return the equivalent ``gupkg`` command line, for display only."""
        parts = ["gupkg"]
        if self.scope != Scope.AUTO:
            parts += ["--scope", self.scope.value]
        parts.append(self.command)
        if self.command == "install":
            parts += [f"--{flag}" for flag, on in (
                ("allow-downgrade", self.allow_downgrade),
                ("refresh-app", self.refresh_app),
                ("no-checksum", self.no_checksum),
            ) if on]
            parts += ["--shim-linkage", self.shim_linkage]
        elif self.command == "update":
            parts += [f"--{flag}" for flag, on in (
                ("check-only", self.check_only),
                ("download-only", self.download_only),
                ("no-checksum", self.no_checksum),
            ) if on]
            if not (self.check_only or self.download_only):
                parts += ["--shim-linkage", self.shim_linkage]
        elif self.command == "config-fix":
            if not self.backup:
                parts.append("--no-backup")
            parts += ["--import-shortcuts", "true" if self.import_shortcuts else "false"]
            if self.output is not None:
                parts += ["--output", str(self.output)]
        if self.path is not None:
            parts.append(str(self.path))
        return " ".join(parts)


def resolve_package(
    path: Path | None, system: System, *, command: str, allow_legacy_directory: bool = False
) -> tuple[Path | None, PackageIdentity | None, Outcome | None]:
    """Classify a package argument and resolve its version directory.

    Parameters
    ----------
    path : Path, optional
        Explicit package argument, or ``None`` for the current directory.
    system : System
        Boundary used to read ``current`` junctions.
    command : str
        Command name used in a failure outcome.
    allow_legacy_directory : bool, default=False
        Whether ``config-fix`` may select a recognized legacy directory that
        has no valid version identity.

    Returns
    -------
    tuple
        ``(directory, identity, failure_outcome)``; exactly one of ``directory``
        and ``failure_outcome`` is ``None``.
    """
    candidate = (path or Path.cwd()).expanduser()
    try:
        identity, _ = identity_from_path(candidate, system)
        return identity.version_path, identity, None
    except OSError as exc:
        return None, None, failure(command, str(exc))
    except ValueError as exc:
        resolution_error = str(exc)

    # Ambiguous package roots and broken ``current`` entries are never
    # reinterpreted as legacy directories.
    if (
        not allow_legacy_directory
        or "multiple version directories" in resolution_error
        or '"current" path' in resolution_error
    ):
        return None, None, failure(command, resolution_error)
    candidate = candidate.resolve()
    if not candidate.is_dir():
        return None, None, failure(command, f"Package directory does not exist: {candidate}")

    # config-fix run inside a folder that holds App or pkg.toml but is not yet
    # a discoverable version (no pkg.toml) treats that folder as the version.
    if (
        command == "config-fix"
        and is_version_directory_name(candidate.name)
        and ((candidate / "App").is_dir() or (candidate / "pkg.toml").is_file())
    ):
        identity = PackageIdentity.from_version_path(candidate.parent, candidate, is_current=False)
        return candidate, identity, None
    if not (pick_legacy_metadata_files(candidate) or has_legacy_metadata(candidate)):
        return None, None, failure(command, resolution_error)
    return candidate, None, None


def run_package_command(request: PackageRequest, system: System) -> Outcome:
    """Run one package operation and describe its result.

    Parameters
    ----------
    request : PackageRequest
        The operation and its options.
    system : System
        Boundary for every machine side effect the operation has.

    Returns
    -------
    Outcome
        Result with a ``package`` or ``config`` record for display. The
        operation's events are reported inside the package's scope, so the
        package's log receives them.
    """
    command = request.command
    directory, identity, error = resolve_package(
        request.path, system, command=command, allow_legacy_directory=command == "config-fix"
    )
    if error is not None:
        return error
    assert directory is not None

    # A legacy folder without an identity has no package of its own; every
    # other operation reports inside its package's scope, so the package's
    # log (and the run log's link to it) receives the events.
    if identity is None:
        return _run_resolved(request, system, directory, identity)
    with package_scope(
        identity.name, identity.package_root, root=identity.package_root.parent, sole=True
    ) as scope:
        outcome = _run_resolved(request, system, directory, identity)
        scope.result.update(ok=outcome.result.ok, status=outcome.status)
    return outcome


def _run_resolved(
    request: PackageRequest, system: System, directory: Path, identity: PackageIdentity | None
) -> Outcome:
    """Run the operation of *request* on the package already resolved at *directory*."""
    command = request.command
    if command == "config-fix":
        return run_guarded_outcome(
            lambda: fix_package_config(
                directory,
                identity,
                backup=request.backup,
                import_shortcuts=request.import_shortcuts,
                output=request.output,
            ),
            command,
        )
    if command == "config-check":
        outcome = run_guarded(
            command, lambda: package_workflows.health_check_package(directory, system, scope=request.scope)
        )
        outcome.data["config"] = {"path": _path_text(directory), "operation": "check"}
        return outcome
    if command == "install":
        operation = lambda: package_workflows.install_package(  # noqa: E731
            directory,
            system,
            scope=request.scope,
            allow_downgrade=request.allow_downgrade,
            refresh_app=request.refresh_app,
            no_checksum=request.no_checksum,
            shim_linkage=request.shim_linkage,
        )
    elif command == "update" and request.check_only:
        operation = lambda: package_workflows.check_package_update(directory, system)  # noqa: E731
    elif command == "update" and request.download_only:
        operation = lambda: package_workflows.download_package_update(  # noqa: E731
            directory, system, no_checksum=request.no_checksum
        )
    elif command == "update":
        operation = lambda: package_workflows.full_package_upgrade(  # noqa: E731
            directory,
            system,
            scope=request.scope,
            no_checksum=request.no_checksum,
            shim_linkage=request.shim_linkage,
        )
    else:
        return failure(command, f"Unsupported package command: {command}")
    outcome = run_guarded(command, operation)
    outcome.data["package"] = _command_package_record(directory, request.scope, system)
    return outcome


def run_guarded_outcome(operation: Callable[[], Outcome], command: str) -> Outcome:
    """Run an operation that already returns an outcome, mapping exceptions to failed outcomes."""
    try:
        outcome = operation()
    except OSError as exc:
        outcome = failure(command, str(exc), EXIT_MUTATION_ERROR)
    except Exception as exc:  # pragma: no cover - defensive process boundary
        outcome = failure(command, f"Unexpected internal error: {exc}", EXIT_INTERNAL_ERROR)
    emit_problems(outcome.result.warnings, outcome.result.errors)
    return outcome


def _command_package_record(path: Path, scope: Scope, system: System) -> dict[str, Any]:
    """Describe a package after a command: the selected version and what ``current`` activates."""
    try:
        identity, _ = identity_from_path(path, system)
    except (OSError, ValueError):
        return {"path": _path_text(path), "scope": scope.value}
    return {
        "path": _path_text(identity.version_path),
        "identity": identity.name,
        "scope": scope.value,
        "version": identity.version_string,
        "installed_version": installed_version(identity.package_root, system),
    }


@dataclass(frozen=True)
class PackageSummary:
    """Display text for a selected package: title, description, and any warning."""

    title: str
    description: str
    warning: str


def package_summary(path_text: str, system: System) -> PackageSummary:
    """Summarize the package at *path_text* (empty means the current directory)."""
    try:
        identity, _ = identity_from_path(Path(path_text or ".").expanduser(), system)
        config_path = identity.version_path / "pkg.toml"
        config = read_toml_file(config_path) if config_path.exists() else {}
        installed = installed_version(identity.package_root, system) or "not installed"
        conflicts = check_metadata_consistency(identity, config)
        description = config.get("description", "")
        return PackageSummary(
            f"{identity.name} {identity.version_string}  Installed: {installed}",
            description if isinstance(description, str) else "",
            "Warning: pkg.toml metadata conflicts with the directory name." if conflicts else "",
        )
    except (OSError, TypeError, ValueError, ConfigValidationError):
        return PackageSummary("No package selected", "Enter a package path to see its summary.", "")


def automatic_scope(path_text: str, system: System) -> tuple[Scope, bool] | None:
    """Return the scope ``auto`` would pick for a package, and whether System is available."""
    try:
        identity, _ = identity_from_path(Path(path_text or ".").expanduser(), system)
    except (OSError, ValueError):
        return None
    system_available = system.is_elevated() and not identity.only_portable_by_name
    return (Scope.MACHINE if system_available else Scope.USER), system_available


# ---------------------------------------------------------------------------
# Manager configuration
# ---------------------------------------------------------------------------


def load_manager(system: System, explicit: Path | None = None) -> ManagerConfig:
    """Load the manager configuration from ``--config`` or the fixed search locations.

    Parameters
    ----------
    system : System
        Boundary that answers whether the run is elevated and the folder admin-only.
    explicit : Path, optional
        ``--config`` value.

    Raises
    ------
    ConfigValidationError
        If no file is found (the message lists every searched location) or the
        selected file is invalid.
    """
    selected = discover_manager_config(explicit)
    if selected is None:
        searched = ", ".join(str(path) for path in manager_config_candidates())
        raise ConfigValidationError(
            f"No manager configuration found; searched: {searched}. "
            "Create one with 'gupkg manager init'."
        )

    # The configuration names the roots, executables, and registry source an
    # elevated run will act on, so an elevated run reads it only from a
    # directory that non-administrators cannot modify.
    if is_elevated_run(system):
        try:
            require_admin_only(system, Path(selected).resolve().parent)
        except UntrustedLocationError as exc:
            raise ConfigValidationError(f"Refusing the manager configuration {selected}: {exc}") from exc
    return load_manager_config(selected)


def save_manager_config(config: ManagerConfig) -> ManagerConfig:
    """Validate *config*, then write it atomically and return it re-read.

    The text is checked in a scratch file beside the target first, so a
    configuration that fails validation never replaces or creates the real file.
    """
    text = manager_config_text(config)
    scratch = config.path.with_name(config.path.name + ".check")
    write_text_atomic(scratch, text)
    try:
        load_manager_config(scratch)
    finally:
        scratch.unlink(missing_ok=True)
    write_text_atomic(config.path, text)
    return load_manager_config(config.path)


def init_manager(
    path: Path | None = None, *, force: bool = False, config: ManagerConfig | None = None
) -> tuple[ManagerConfig, list[str]]:
    """Create a manager configuration and the folders it names.

    Parameters
    ----------
    path : Path, optional
        Configuration file to create; default ``gupkg-config.toml`` in the gupkg package directory.
    config : ManagerConfig, optional
        Reviewed or edited settings to write; the defaults when omitted.
    force : bool, default=False
        Overwrite an existing file.

    Returns
    -------
    tuple[ManagerConfig, list[str]]
        The validated configuration and warnings for folders that could not be
        created (for example the system root without Administrator rights).

    Raises
    ------
    ConfigValidationError
        If the file already exists and *force* is false, or the environment
        does not identify the default locations.
    """
    config = config or default_manager_config(path)
    if config.path.exists() and not force:
        raise ConfigValidationError(
            f"Manager configuration already exists: {config.path} (use --force to replace it)"
        )
    saved = save_manager_config(config)
    warnings: list[str] = []
    for folder in (saved.user_root, saved.user_bin, saved.system_root, saved.system_bin):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warnings.append(f"Could not create {folder}: {exc}")
    return saved, warnings


def manager_init_outcome(path: Path | None, *, force: bool) -> Outcome:
    """Run :func:`init_manager` as a command outcome."""
    try:
        config, warnings = init_manager(path, force=force)
    except (ConfigValidationError, ValueError) as exc:
        return failure("manager.init", str(exc))
    except OSError as exc:
        return failure("manager.init", str(exc), EXIT_MUTATION_ERROR)
    return Outcome(
        "manager.init",
        ActionResult(ok=True, changed=True, warnings=warnings, status="created"),
        {"manager": {
            "config": _path_text(config.path),
            "user_packages": config.user_root,
            "system_packages": config.system_root,
            "registry_cache": config.registry_cache,
        }},
    )


# ---------------------------------------------------------------------------
# Manager inventory and bulk updates
# ---------------------------------------------------------------------------


def selected_scopes(value: str) -> set[Scope]:
    """Resolve an aggregate manager scope, where ``auto`` means both roots."""
    return {Scope.USER, Scope.MACHINE} if value == "auto" else {Scope(value)}


def target_record(target: Any, result: ActionResult | None = None) -> dict[str, Any]:
    """Serialize one manager target, with its action result when one exists."""
    return {
        "id": target.target_id,
        "selector": target.package.selector,
        "scope": target.scope.value,
        "installation": target.installation_status,
        "installed_version": target.installed_version,
        "health": target.health_status,
        "checked_at": target.checked_at,
        "status": target.update_status if result is None else (result.status or ("ok" if result.ok else "failed")),
        "changed": False if result is None else result.changed,
        "exit_code": 0 if result is None else result.exit_code,
        "warnings": [] if result is None else result.warnings,
        "errors": target.diagnostics if result is None else result.errors,
    }


def filter_targets(inventory: ManagerInventory, scopes: set[Scope], status: str = "all") -> list[Any]:
    """Return targets in *scopes* matching a status filter.

    *status* is ``all``, ``installed``, ``uninstalled``, ``updatable``, or
    ``unhealthy`` (case-insensitive).
    """
    matches = {
        "all": lambda target: True,
        "installed": lambda target: target.installation_status == "installed",
        "uninstalled": lambda target: target.installation_status == "not-installed",
        "updatable": lambda target: target.update_status == "available",
        "unhealthy": lambda target: target.health_status != "healthy",
    }[status.casefold()]
    return [t for t in inventory.targets if t.scope in scopes and matches(t)]


def diagnostic_lines(config: ManagerConfig, inventory: ManagerInventory) -> list[str]:
    """Return the local diagnostics ``manager doctor`` shows, one per line."""
    lines = [f"Manager: {config.path}"]
    for scope in inventory.scopes:
        lines.append(f"{'User' if scope.scope == Scope.USER else 'System'} root: {'complete' if scope.complete else 'incomplete'}")
        lines.extend(scope.diagnostics)
    lines.extend(
        f"{target.target_id}: " + "; ".join(target.diagnostics)
        for target in inventory.targets
        if target.diagnostics
    )
    return lines


def refresh_update_status(
    inventory: ManagerInventory,
    system: System,
    *,
    on_progress: Callable[[int, int, ManagedTarget, ActionResult | None], None] | None = None,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> list[ActionResult]:
    """Check every target's update source one at a time and record its status.

    Checks run sequentially because each one captures process-wide output.
    ``on_progress(index, total, target, None)`` is called before each check and
    ``on_progress(index, total, target, result)`` after it, with a 1-based
    index. When ``cancel_requested()`` becomes true the loop stops before the
    next target; results already obtained are returned.
    """
    results: list[ActionResult] = []
    total = len(inventory.targets)
    for index, target in enumerate(inventory.targets, start=1):
        if cancel_requested():
            break
        if on_progress is not None:
            on_progress(index, total, target, None)
        result = manager_update_target(target, system)
        results.append(result)
        if on_progress is not None:
            on_progress(index, total, target, result)
    return results


def needs_elevation(plan: UpgradePlan, system: System) -> bool:
    """Return whether the plan changes the system scope from a non-elevated process."""
    return any(
        entry.outcome == "eligible" and entry.target.scope == Scope.MACHINE for entry in plan.entries
    ) and not system.is_elevated()


def relaunch_elevated_update(config: ManagerConfig, system: System, *, no_checksum: bool) -> bool:
    """Ask Windows to repeat a confirmed bulk update elevated; return whether it was accepted."""
    return system.relaunch_elevated(elevated_update_arguments(config, no_checksum=no_checksum))


def list_outcome(
    inventory: ManagerInventory, scope: str, *, doctor: bool = False, filter_name: str = "all"
) -> Outcome:
    """Report filtered inventory (or doctor health) without contacting providers."""
    scopes = selected_scopes(scope)
    selected = sorted(
        filter_targets(inventory, scopes, filter_name),
        key=lambda item: (item.target_id.casefold(), item.target_id),
    )
    targets = [target_record(target) for target in selected]

    # Doctor additionally requires every selected target to be healthy.
    complete = all(s.complete for s in inventory.scopes if s.scope in scopes)
    if doctor:
        complete = complete and all(record["health"] == "healthy" for record in targets)
    return Outcome(
        "manager.doctor" if doctor else "manager.list",
        ActionResult(ok=complete, exit_code=EXIT_SUCCESS if complete else EXIT_USER_ERROR, status="listed"),
        {"manager": {"complete": complete, "target_count": len(targets)}, "targets": targets},
    )


def self_integrity_problems() -> list[str]:
    """Return the running gupkg's differences from its MANIFEST.sha256, for ``manager doctor``.

    A version that is not an installed release (a source checkout has no
    manifest) yields no problems, so doctor stays usable in development.
    """
    from .distribution import standalone_version_root
    from .integrity import ManifestError, verify_version_directory

    try:
        report = verify_version_directory(standalone_version_root())
    except (ConfigValidationError, ManifestError, OSError) as exc:
        return [f"gupkg's own files could not be checked: {exc}"]
    return [] if not report.manifest_found else report.problems()


def plan_updates(
    inventory: ManagerInventory,
    scope: str,
    system: System,
    *,
    on_progress: Callable[[int, int, ManagedTarget, ActionResult | None], None] | None = None,
) -> UpgradePlan:
    """Check every selected installed target and plan updates; nothing is downloaded.

    ``on_progress`` has the signature described in :func:`refresh_update_status`
    and is called only for targets that are actually checked.
    """
    scopes = selected_scopes(scope)
    complete = all(item.complete for item in inventory.scopes if item.scope in scopes)
    checkable = [
        target for target in inventory.targets
        if complete
        and target.scope in scopes
        and target.installation_status == "installed"
        and target.health_status == "healthy"
    ]
    total = len(checkable)
    done = 0

    def check(target: ManagedTarget) -> ActionResult:
        """Check one target, reporting progress around the call."""
        nonlocal done
        done += 1
        if on_progress is not None:
            on_progress(done, total, target, None)
        result = manager_update_target(target, system)
        if on_progress is not None:
            on_progress(done, total, target, result)
        return result

    return plan_upgrade_all(inventory, scopes, check)


def has_eligible_updates(plan: UpgradePlan) -> bool:
    """Return whether the plan has anything to download or install."""
    return any(entry.outcome == "eligible" for entry in plan.entries)


def decline_updates(plan: UpgradePlan, reason: str) -> None:
    """Mark every eligible entry failed with *reason* (a declined confirmation).

    The stored successful check result is dropped so a declined or impossible
    confirmation can never look like a successful update.
    """
    for entry in plan.entries:
        if entry.outcome == "eligible":
            entry.result, entry.outcome, entry.reason = None, "failed", reason


def execute_updates(
    plan: UpgradePlan,
    config: ManagerConfig,
    system: System,
    *,
    download_only: bool = False,
    no_checksum: bool = False,
    shim_linkage: str | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    on_progress: Callable[[str, str], None] | None = None,
) -> UpgradePlan:
    """Update (or only stage) every eligible entry, continuing after failures.

    Parameters
    ----------
    plan : UpgradePlan
        Plan from :func:`plan_updates`; entries receive their results.
    config : ManagerConfig
        Supplies roots, bin directories, and the default shim linkage.
    system : System
        Boundary for every machine side effect of the updates.
    download_only : bool, default=False
        Stage new versions without activating them.
    cancel_requested : callable, optional
        Checked between targets; a running target is never interrupted.
    on_progress : callable, optional
        Called as ``on_progress(target_id, "running" | "completed" | "failed")``.
    """
    def operation(target: Any) -> ActionResult:
        """Update one target, reporting its progress."""
        if on_progress:
            on_progress(target.target_id, "running")
        if download_only:
            result = manager_download_target(target, system, no_checksum=no_checksum)
        else:
            result = manager_upgrade_target(
                target,
                config,
                system,
                no_checksum=no_checksum,
                shim_linkage=shim_linkage,
            )
        if on_progress:
            on_progress(target.target_id, "completed" if result.ok else "failed")
        return result

    return execute_upgrade_plan(
        plan,
        lambda target: manager_revalidate_target(target, config, system),
        operation,
        cancel_requested=cancel_requested,
    )


def update_outcome(plan: UpgradePlan, *, mode: str, scope: str) -> Outcome:
    """Report every planned target's outcome.

    Parameters
    ----------
    mode : {"checked", "downloaded", "updated"}
        What the run was allowed to do; it becomes the outcome status.
    """
    records: list[dict[str, Any]] = []
    for entry in plan.entries:
        result = entry.result
        if result is None and entry.outcome == "failed":
            result = ActionResult(ok=False, errors=[entry.reason or "manager target failed"], exit_code=EXIT_USER_ERROR)
        record = target_record(entry.target, result)
        if result is None:
            record["status"] = entry.reason or entry.outcome
        if entry.target.candidate_version is not None:
            record["candidate_version"] = entry.target.candidate_version
        records.append(record)
    highest = max([EXIT_SUCCESS, *(record["exit_code"] for record in records)])
    return Outcome(
        "manager.update",
        ActionResult(
            ok=highest == EXIT_SUCCESS,
            changed=any(record["changed"] for record in records),
            exit_code=highest,
            status=mode,
        ),
        {
            "manager": {"target_count": len(records), "scope": scope},
            "targets": records,
            "summary": {
                "total": len(records),
                "changed": sum(record["changed"] for record in records),
                "failed": sum(record["exit_code"] != 0 for record in records),
            },
        },
    )


def plan_summary_lines(plan: UpgradePlan) -> list[str]:
    """Return the counts and per-target lines a front end shows before confirming."""
    entries = plan.entries
    lines = [
        f"Available: {sum(e.outcome == 'eligible' for e in entries)}",
        f"Current: {sum(e.reason == 'current' for e in entries)}",
        f"Skipped: {sum(e.outcome == 'skipped' and e.reason != 'current' for e in entries)}",
        f"Failed checks: {sum(e.outcome == 'failed' for e in entries)}",
        "",
    ]
    lines.extend(
        f"{e.target.target_id}: {e.outcome}" + (f" ({e.reason})" if e.reason else "") for e in entries
    )
    return lines


def execution_summary_line(plan: UpgradePlan) -> str:
    """Return the one-line totals shown after a batch finishes."""
    entries = plan.entries
    return (
        f"Summary: {sum(e.outcome == 'upgraded' for e in entries)} upgraded, "
        f"{sum(e.outcome == 'failed' for e in entries)} failed, "
        f"{sum(e.outcome in {'skipped', 'not-attempted'} for e in entries)} skipped/not attempted."
    )


def elevated_update_arguments(config: ManagerConfig, *, no_checksum: bool) -> list[str]:
    """Return the argument vector that repeats a confirmed bulk update elevated."""
    arguments = ["manager", "--config", str(config.path), "update", "--yes"]
    if no_checksum:
        arguments.append("--no-checksum")
    return arguments


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _sync_registry_phase(config: ManagerConfig, system: System) -> ActionResult:
    """Sync the configured registry cache, reported as the ``Sync registry`` phase."""
    with phase("Sync registry") as syncing:
        return syncing.settle(
            sync_registry(config.registry_cache, system, source=config.registry_source)
        )


def registry_sync_outcome(config: ManagerConfig, system: System) -> Outcome:
    """Download the latest registry archive into the configured cache."""
    outcome = run_guarded("manager.registry.sync", lambda: _sync_registry_phase(config, system))
    outcome.data["registry"] = {"cache": _path_text(config.registry_cache), "source": config.registry_source}
    return outcome


def registry_status_outcome(config: ManagerConfig, system: System) -> Outcome:
    """Report the active cached registry revision without any download."""
    try:
        state = registry_status(config.registry_cache, system)
    except ConfigValidationError as exc:
        return failure("manager.registry.status", str(exc))
    return Outcome(
        "manager.registry.status",
        ActionResult(True, status="current"),
        {"registry": {
            "cache": _path_text(config.registry_cache),
            "source": config.registry_source,
            "revision": state.revision,
            "synchronized_at": state.synchronized_at,
        }},
    )


def _ensure_registry(
    command: str, config: ManagerConfig, system: System, offline: bool
) -> Outcome | None:
    """Make sure a cached registry exists, downloading only when allowed.

    Returns a failure outcome when no usable cache is available, else ``None``.
    """
    try:
        cached = registry_status(config.registry_cache, system).tree_path is not None
    except ConfigValidationError as exc:
        return failure(command, str(exc))
    if cached:
        return None
    if offline:
        return failure(command, "--offline requires a usable cached registry tree")
    sync = run_guarded(command, lambda: _sync_registry_phase(config, system))
    return None if sync.result.ok else sync


def registry_search_outcome(
    config: ManagerConfig, system: System, query: str, *, offline: bool
) -> Outcome:
    """Search the registry by name, downloading it first only when none is cached."""
    problem = _ensure_registry("manager.search", config, system, offline)
    if problem is not None:
        return problem
    try:
        matches = search_registry(config.registry_cache, system, query)
    except (FileNotFoundError, ValueError, ConfigValidationError) as exc:
        return failure("manager.search", str(exc))
    return Outcome(
        "manager.search",
        ActionResult(True, status="searched"),
        {
            "registry": {
                "cache": _path_text(config.registry_cache),
                "revision": registry_status(config.registry_cache, system).revision,
            },
            "registry_packages": [
                {"selector": item.selector, "path": _path_text(item.version_path)} for item in matches
            ],
        },
    )


def install_registry_package(
    config: ManagerConfig,
    system: System,
    selector: str,
    scope: Scope,
    *,
    offline: bool = False,
) -> Outcome:
    """Install one registry package by exact name into the configured root.

    Parameters
    ----------
    system : System
        Boundary for every machine side effect of the install.
    selector : str
        Exact (case-insensitive) registry name, never a path.
    scope : Scope
        ``Scope.USER`` or ``Scope.MACHINE``; a new package has no owner yet.
    offline : bool, default=False
        Require an already cached registry; never download.
    """
    command = "manager.install"
    if scope == Scope.AUTO:
        return failure(command, "manager install requires --scope user or --scope system")
    if (
        any(part in selector for part in ("/", "\\"))
        or Path(selector).is_absolute()
        or Path(selector).drive
        or selector in {".", "..", "~"}
    ):
        return failure(command, "Registry selectors cannot be paths")
    try:
        problem = _ensure_registry(command, config, system, offline)
        if problem is not None:
            return problem
        package = resolve_selector(config.registry_cache, system, selector)

        # Stage the validated seed below the selected root, then hand it to
        # the ordinary installer with manager-owned destinations.
        target = config.root(scope) / package.selector / package.version_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copytree(package.version_path, target)
    except (FileNotFoundError, ValueError, ConfigValidationError) as exc:
        return failure(command, str(exc))
    except OSError as exc:
        return failure(command, str(exc), EXIT_MUTATION_ERROR)
    with package_scope(package.selector, target.parent, root=config.root(scope), sole=True) as package_run:
        outcome = run_guarded(
            command,
            lambda: package_workflows.install_package(
                target,
                system,
                scope=scope,
                install_context=installation_context(config, scope, system),
                shim_linkage=config.shim_linkage,
            ),
        )
        package_run.result.update(ok=outcome.result.ok, status=outcome.status)
    outcome.data["manager"] = {"selector": package.selector, "scope": scope.value, "path": _path_text(target)}
    return outcome


# ---------------------------------------------------------------------------
# gupkg's own installation
# ---------------------------------------------------------------------------


def self_outcome(
    config: ManagerConfig,
    system: System,
    action: str,
    *,
    scope: Scope = Scope.USER,
    shim_linkage: str | None = None,
) -> Outcome:
    """Report on (``status``), repair (``repair``) or update (``update``) gupkg itself."""
    from .distribution import repair_self, self_status, update_self

    command = f"manager.self.{action}"
    if action == "status":
        try:
            report = self_status()
        except (OSError, ConfigValidationError, ValueError) as exc:
            return failure(command, str(exc), EXIT_MUTATION_ERROR)
        integrity = report["integrity"]
        healthy = bool(report["runtime_healthy"]) and integrity.ok
        problems = integrity.problems()
        if problems:
            problems.append("Reinstall gupkg from a release ZIP (gupkg-bootstrap.cmd) to fix this.")
        return Outcome(
            command,
            ActionResult(
                healthy,
                errors=problems,
                status="healthy" if healthy else "unhealthy",
                exit_code=EXIT_SUCCESS if healthy else EXIT_MUTATION_ERROR,
            ),
            _self_record(report),
        )

    # Update installs a newer verified release; the new version, not the running
    # one, is then the active install, so no diagnostics of the old one follow.
    scope = Scope.USER if scope == Scope.AUTO else scope
    if action == "update":
        outcome = run_guarded(
            command,
            lambda: update_self(
                system,
                scope=scope,
                install_context=installation_context(config, scope, system, shim_linkage=shim_linkage),
            ),
        )
        outcome.data = {"self": {"operation": action}}
        return outcome

    # Repair reinstalls the running version's shims and PATH entries.
    outcome = run_guarded(
        command,
        lambda: repair_self(
            system,
            scope=scope,
            install_context=installation_context(config, scope, system, shim_linkage=shim_linkage),
        ),
    )
    try:
        outcome.data = _self_record(self_status(), operation=action)
    except (OSError, ConfigValidationError, ValueError):
        outcome.data = {"self": {"operation": action}}
    return outcome


def _self_record(report: dict[str, Any], *, operation: str | None = None) -> dict[str, Any]:
    """Serialize standalone runtime and shim diagnostics."""
    self_data: dict[str, Any] = {"operation": operation}
    for key in ("version_root", "runtime"):
        if report.get(key) is not None:
            self_data[key] = _path_text(Path(report[key]))
    if "runtime_healthy" in report:
        self_data["runtime_healthy"] = bool(report["runtime_healthy"])
    if "integrity" in report:
        integrity = report["integrity"]
        self_data["manifest_found"] = integrity.manifest_found
        self_data["files_modified"] = len(integrity.modified)
        self_data["files_missing"] = len(integrity.missing)
        self_data["files_extra"] = len(integrity.extra)
    shims = sorted(
        (
            {
                "scope": shim.scope.value,
                "path": _path_text(shim.path),
                "config_path": _path_text(shim.config_path),
                "target": shim.target,
                "healthy": bool(shim.healthy),
                "diagnostic": shim.diagnostic,
            }
            for shim in report.get("shims", [])
        ),
        key=lambda item: (item["scope"], item["path"]),
    )
    return {"self": self_data, "self_shims": shims}


__all__ = [
    "PackageRequest",
    "PackageSummary",
    "automatic_scope",
    "decline_updates",
    "package_summary",
    "diagnostic_lines",
    "filter_targets",
    "needs_elevation",
    "refresh_update_status",
    "relaunch_elevated_update",
    "discover_manager",
    "elevated_update_arguments",
    "execute_updates",
    "execution_summary_line",
    "has_eligible_updates",
    "init_manager",
    "install_registry_package",
    "list_outcome",
    "load_manager",
    "manager_init_outcome",
    "plan_summary_lines",
    "plan_updates",
    "registry_search_outcome",
    "registry_status_outcome",
    "registry_sync_outcome",
    "resolve_package",
    "run_guarded",
    "run_package_command",
    "save_manager_config",
    "selected_scopes",
    "self_integrity_problems",
    "self_outcome",
    "target_record",
    "update_outcome",
]
