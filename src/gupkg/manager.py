"""Load fixed-location manager configuration, build inventories, and run batches.

The manager domain validates one small TOML schema, expands paths without a
shell, and delegates package traversal to :func:`discover_collection`. Loading
configuration and inventory is read-only and never creates a configured root.
Batch updates are planned without downloading or activating anything, then
executed target by target with revalidation and per-target failure isolation.

Usage and API
-------------
Call ``discover_manager_config(...)`` and ``load_manager_config(...)``, then
``discover_manager(...)`` for a scoped inventory. ``plan_upgrade_all(...)`` and
``execute_upgrade_plan(...)`` form the batch-update boundary shared by the CLI
and the manager TUI; ``manager_update_target(...)``,
``manager_revalidate_target(...)``, ``manager_upgrade_target(...)``, and
``manager_download_target(...)`` are the per-target operations they use.

Implementation Approach
-----------------------
Every target is a package discovered below exactly one configured root, and
that root fixes its scope. Per-target operations call the ordinary package
workflows with the manager's install context and capture their progress
output, so callers own presentation.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import urllib.parse
from datetime import datetime, timezone
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from .collection import DiscoveredPackage, Inventory, discover_collection
from .configuration import read_runtime_config
from .core import (
    ActionResult,
    ConfigValidationError,
    EXIT_INTERNAL_ERROR,
    EXIT_MUTATION_ERROR,
    EXIT_USER_ERROR,
    PackageIdentity,
    Scope,
    compare_package_versions,
    read_toml_file,
)
from .catalog.catalog import CatalogError, resolve_package_path
from .catalog.local import read_current
from .model.mode import ModeContext
from .modes import scope_context
from .registry import OFFICIAL_SOURCE
from .tomlio import dumps
from .system.port import System


@dataclass(frozen=True)
class ManagerConfig:
    """Describe validated manager configuration and its scoped destinations."""

    path: Path
    system_root: Path
    user_root: Path
    system_bin: Path | None = None
    user_bin: Path | None = None
    registry_cache: Path = field(default_factory=lambda: default_registry_cache())
    registry_source: str = OFFICIAL_SOURCE
    shim_linkage: str = "dynamic"
    schema_version: int = 2

    def root(self, scope: Scope) -> Path:
        """Return the configured package root that owns *scope*."""
        return self.user_root if scope == Scope.USER else self.system_root


@dataclass
class ManagedTarget:
    """Describe one scoped package available in a manager root."""

    target_id: str
    scope: Scope
    package: DiscoveredPackage
    installation_status: str
    installed_version: str | None
    local_version: str | None
    health_status: str
    update_status: str = "unchecked"
    candidate_version: str | None = None
    diagnostics: list[str] = field(default_factory=list)
    checked_at: str | None = None


@dataclass
class ManagedScope:
    """Describe discovery for one configured scope, including incomplete roots."""

    scope: Scope
    root: Path
    inventory: Inventory | None
    complete: bool
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class ManagerInventory:
    """Contain deterministic scoped targets and root-level diagnostics."""

    config: ManagerConfig
    scopes: list[ManagedScope]
    targets: list[ManagedTarget]


@dataclass
class UpgradePlanEntry:
    """Record one target's planned and, after execution, actual batch outcome."""

    target: ManagedTarget
    outcome: str
    reason: str | None = None
    result: ActionResult | None = None


@dataclass
class UpgradePlan:
    """Contain a deterministic, non-mutating manager upgrade plan."""

    entries: list[UpgradePlanEntry]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Windows-style ``%NAME%`` references accepted in manager paths.
_VARIABLE_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def discover_manager_config(
    explicit: Path | None = None,
    *,
    module_directory: Path | None = None,
) -> Path | None:
    """Select the manager file: ``--config`` or the one beside the gupkg package.

    Parameters
    ----------
    explicit : Path, optional
        ``--config`` value; returned as-is when given.
    module_directory : Path, optional
        Directory of the installed ``gupkg`` package; defaults to this one.

    Returns
    -------
    Path or None
        The explicit path, or the configuration file in the package directory
        when it exists.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    for candidate in manager_config_candidates(module_directory=module_directory):
        if candidate.is_file():
            return candidate
    return None


def manager_config_candidates(*, module_directory: Path | None = None) -> list[Path]:
    """Return the only location searched for ``gupkg-config.toml``.

    The configuration decides package roots, executable directories, and the
    registry source, and an elevated run acts on all of them, so it is read only
    from the installed package directory (admin-only for a system install). The
    working directory, ``GUPKG_HOME``, and per-user folders such as
    ``%APPDATA%`` are never searched.
    """
    directory = Path(module_directory) if module_directory else Path(__file__).resolve().parent
    return [directory / "gupkg-config.toml"]


def load_manager_config(path: Path) -> ManagerConfig:
    """Read and strictly validate one schema-version-two manager file.

    Parameters
    ----------
    path : Path
        Manager configuration file. Relative configured paths resolve against
        its directory.

    Returns
    -------
    ManagerConfig
        Validated configuration with absolute, expanded paths.

    Raises
    ------
    ConfigValidationError
        If the file is missing, unreadable, or violates the schema.
    """
    path = Path(path).expanduser().absolute()
    if not path.is_file():
        raise ConfigValidationError(f"Manager configuration is not a regular file: {path}")
    try:
        raw = read_toml_file(path)
    except Exception as exc:
        raise ConfigValidationError(f"Could not read manager configuration {path}: {exc}") from exc

    # Only the current schema is accepted; older files must be migrated.
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != 2:
        raise ConfigValidationError(
            "schema_version must be the integer 2; older manager configurations "
            "must be explicitly migrated"
        )
    _exact_table_keys(raw, {"mode", "schema_version", "packages", "bin"}, "manager configuration", optional={"registry", "shims", "self"})
    if raw["mode"] != "manager":
        raise ConfigValidationError("mode must be exactly 'manager'")

    # Package roots and bin directories come in user/system pairs that must
    # not collide; the registry cache must live outside both package roots.
    _exact_table_keys(raw["packages"], {"system", "user"}, "[packages]")
    _exact_table_keys(raw["bin"], {"system", "user"}, "[bin]")
    registry = raw.get("registry", {})
    _exact_table_keys(registry, set(), "[registry]", optional={"cache", "source"})
    shims = raw.get("shims", {"linkage": "dynamic"})
    _exact_table_keys(shims, {"linkage"}, "[shims]")
    # Configurations written before 0.18 may carry [self] dependency_update_check.
    # The advisory check no longer exists, so the table is accepted and ignored.
    _exact_table_keys(raw.get("self", {}), set(), "[self]", optional={"dependency_update_check"})
    roots = {name: _expand_manager_path(value, path.parent, f"packages.{name}") for name, value in raw["packages"].items()}
    bins = {name: _expand_manager_path(value, path.parent, f"bin.{name}") for name, value in raw["bin"].items()}
    cache = (
        _expand_manager_path(registry["cache"], path.parent, "registry.cache")
        if "cache" in registry
        else default_registry_cache()
    )
    source = registry.get("source", OFFICIAL_SOURCE)
    if not isinstance(source, str) or urllib.parse.urlparse(source).scheme.lower() not in {"http", "https", "file"}:
        raise ConfigValidationError(
            "[registry].source must be an http(s):// or file: URL of a ZIP archive"
        )
    if _same_or_nested(roots["system"], roots["user"]):
        raise ConfigValidationError("Configured system and user roots must be distinct and non-nested")
    if bins["system"] == bins["user"]:
        raise ConfigValidationError("Configured system and user bin directories must be distinct")
    # The cache may not be mistaken for a package, so it stays outside the roots,
    # except inside the ``App`` folder of a package version (collections never
    # look inside a package).
    for root in (roots["system"], roots["user"]):
        if _same_or_nested(cache, root) and not _inside_package_app(cache, root):
            raise ConfigValidationError(
                "Registry cache must be outside both package roots (or inside a package version's App folder)"
            )
    if shims["linkage"] not in {"dynamic", "static"}:
        raise ConfigValidationError("[shims].linkage must be exactly 'dynamic' or 'static'")
    for field_name, value in (("bin.system", bins["system"]), ("bin.user", bins["user"]), ("registry.cache", cache)):
        if value.exists() and not value.is_dir():
            raise ConfigValidationError(f"Configured {field_name} path is not a directory: {value}")
    return ManagerConfig(
        path,
        roots["system"],
        roots["user"],
        system_bin=bins["system"],
        user_bin=bins["user"],
        registry_cache=cache,
        registry_source=source,
        shim_linkage=shims["linkage"],
    )


def manager_config_text(config: ManagerConfig) -> str:
    """Render a reviewed schema-version-two manager configuration as TOML.

    Parameters
    ----------
    config : ManagerConfig
        Configuration to render; missing bin and cache paths use the scope
        defaults.

    Returns
    -------
    str
        TOML text that :func:`load_manager_config` accepts.
    """
    # Directories the configuration leaves unset take the same defaults that
    # interactive initialization proposes.
    defaults = default_manager_config(config.path)
    user_bin = config.user_bin or defaults.user_bin
    system_bin = config.system_bin or defaults.system_bin

    def toml_path(value: Path) -> str:
        """Render a Windows path with forward slashes, which TOML and Windows both accept."""
        return str(value).replace("\\", "/")

    return dumps({
        "mode": "manager",
        "schema_version": 2,
        "packages": {"system": toml_path(config.system_root), "user": toml_path(config.user_root)},
        "bin": {"system": toml_path(system_bin), "user": toml_path(user_bin)},
        "registry": {"cache": toml_path(config.registry_cache), "source": config.registry_source},
        "shims": {"linkage": config.shim_linkage},
    })


def default_manager_config(path: Path | None = None) -> ManagerConfig:
    """Build the reviewed manager configuration used by interactive initialization.

    Parameters
    ----------
    path : Path, optional
        Destination for the configuration file; defaults to
        ``gupkg-config.toml`` in the gupkg package directory.

    Returns
    -------
    ManagerConfig
        Defaults for the user and system collections, executable directories,
        and registry cache.

    Raises
    ------
    ValueError
        If the Windows environment does not identify the required default
        locations.
    """
    environment = {name: os.environ.get(name) for name in ("USERPROFILE", "LOCALAPPDATA", "SYSTEMDRIVE")}
    if not all(environment[name] for name in ("USERPROFILE", "LOCALAPPDATA", "SYSTEMDRIVE")):
        raise ValueError(
            "USERPROFILE, LOCALAPPDATA, and SYSTEMDRIVE are required to initialize manager mode"
        )
    system_drive = environment["SYSTEMDRIVE"]
    if len(system_drive) == 2 and system_drive[1] == ":":
        system_drive += "\\"
    user_profile = Path(environment["USERPROFILE"])
    config_path = Path(path) if path is not None else Path(__file__).resolve().parent / "gupkg-config.toml"

    registry_cache = default_registry_cache()
    return ManagerConfig(
        path=config_path,
        system_root=Path(system_drive) / "opt",
        user_root=user_profile / "opt",
        system_bin=Path(system_drive) / "bin",
        user_bin=user_profile / "bin",
        registry_cache=registry_cache,
    )


def default_registry_cache() -> Path:
    """Return the default registry cache, ``App\\registry`` in the running gupkg version.

    The cache lives with the gupkg version that uses it, so an administrator's
    install keeps it in the same admin-only tree as the code that trusts it.
    """
    return Path(__file__).resolve().parent.parent / "App" / "registry"


def installation_context(config: ManagerConfig, scope: Scope, system: System) -> ModeContext:
    """Build the manager-owned destinations for one explicit scope as a ``ModeContext``.

    Parameters
    ----------
    config : ManagerConfig
        Validated manager configuration: its roots and ``bin`` folders replace the defaults.
    scope : Scope
        ``Scope.USER`` or ``Scope.MACHINE``.
    system : System
        Boundary that knows the scope's default Start Menu and ``bin`` folders.

    Returns
    -------
    ModeContext
        Package root, bin directory, and Start Menu root for *scope*.
    """
    if scope == Scope.AUTO:
        raise ValueError("Installation context requires an explicit scope")
    return scope_context(
        scope, system, root=config.root(scope),
        bin_dir=config.user_bin if scope == Scope.USER else config.system_bin,
    )


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def discover_manager(
    config: ManagerConfig, system: System, *, max_depth: int = 8
) -> ManagerInventory:
    """Discover both configured roots and return scoped, deterministic targets.

    Parameters
    ----------
    config : ManagerConfig
        Validated manager configuration.
    system : System
        Boundary used to read each package's ``current`` junction.
    max_depth : int, default=8
        Maximum descent through marked grouping directories.

    Returns
    -------
    ManagerInventory
        Targets sorted by selector and then user-before-system; a missing or
        unreadable root is reported as an incomplete scope.
    """
    scopes: list[ManagedScope] = []
    targets: list[ManagedTarget] = []
    for scope in (Scope.USER, Scope.MACHINE):
        root = config.root(scope)
        try:
            if not root.is_dir():
                raise OSError("root does not exist" if not root.exists() else "root is not a directory")
            inventory = discover_collection(root, max_depth=max_depth)
        except OSError as exc:
            scopes.append(ManagedScope(
                scope, root, None, False,
                [f"Cannot access {scope_name(scope)} root {root}: {exc}"],
            ))
            continue
        scopes.append(ManagedScope(scope, root, inventory, inventory.complete, list(inventory.diagnostics)))
        targets.extend(
            _target_from_catalog(scope, package, inventory.complete, system) for package in inventory.packages
        )
    targets.sort(key=lambda target: (
        target.package.selector.casefold(), target.package.selector, _scope_order(target.scope)
    ))
    return ManagerInventory(config, scopes, targets)


def _target_from_catalog(
    scope: Scope, package: DiscoveredPackage, complete: bool, system: System
) -> ManagedTarget:
    """Describe one discovered package's activation, versions, and health from the catalog's ``current`` reading."""
    diagnostics = list(package.diagnostics)
    try:
        view = resolve_package_path(package.root, system).view
        current = view.local.current
    except CatalogError:
        # Discovery found version directories that the catalog does not count (0.17's ``_bootstrap`` template
        # only): that is not a broken package, so only ``current`` itself is read.
        current = read_current(package.root, system)
    activation = {"valid": "installed", "absent": "not-installed"}.get(current.status, "broken")
    if current.problem:
        diagnostics.append(current.problem)

    # Validate every manifest through the package configuration boundary so
    # inventory health reflects real package semantics, not file presence.
    for manifest in package.manifests:
        identity = PackageIdentity.from_version_path(
            package.root, manifest.version_path, is_current=manifest.version_path == current.target
        )
        try:
            read_runtime_config(identity)
        except Exception as exc:
            diagnostics.append(f"Invalid manifest {manifest.path}: {exc}")
    health = "incomplete" if not complete else ("unhealthy" if diagnostics else "healthy")
    installed_version = current.version
    local_version = max(
        (manifest.version_path.name for manifest in package.manifests),
        key=cmp_to_key(compare_package_versions),
        default=None,
    )

    # Restore the last recorded update check so statuses survive a rescan.
    status, candidate, checked_at = _saved_update_status(package.root, installed_version or local_version)
    return ManagedTarget(
        f"{scope.value}:{package.selector}",
        scope,
        package,
        activation,
        installed_version,
        local_version,
        health,
        update_status=status,
        candidate_version=candidate,
        diagnostics=diagnostics,
        checked_at=checked_at,
    )


def _saved_update_status(package_root: Path, active: str | None) -> tuple[str, str | None, str | None]:
    """Read a package's last recorded update check without modifying any file.

    Returns
    -------
    tuple of (str, str or None, str or None)
        ``(update_status, candidate_version, checked_at)``. A missing or
        unreadable state file gives ``unchecked``; a failed last attempt gives
        ``error``; an ``available`` result whose candidate is no newer than the
        active version (it was installed after the check) gives ``current``.
    """
    from .updates import update_paths

    # Discovery is read-only: a corrupt state file is left for the update
    # workflow, which owns (and renames) it.
    try:
        state = read_toml_file(update_paths(package_root)["state"])
    except Exception:
        return "unchecked", None, None
    checked_at = state.get("lastSuccessfulCheck") or state.get("lastAttemptedCheck")
    if state.get("lastError"):
        return "error", None, checked_at
    status = state.get("lastStatus")
    if status in {"current", "not-configured"}:
        return status, None, checked_at
    candidate = state.get("lastCandidateVersion")
    if status != "available" or not candidate:
        return "unchecked", None, None
    if active and compare_package_versions(active, candidate) >= 0:
        return "current", None, checked_at
    return "available", candidate, checked_at


def scope_name(scope: Scope) -> str:
    """Return the user-facing manager scope name, ``User`` or ``System``."""
    return "User" if scope == Scope.USER else "System"


def _scope_order(scope: Scope) -> int:
    """Sort user targets before system targets."""
    return 0 if scope == Scope.USER else 1


# ---------------------------------------------------------------------------
# Batch planning and execution
# ---------------------------------------------------------------------------


def plan_upgrade_all(
    inventory: ManagerInventory,
    scopes: set[Scope],
    check_target,
) -> UpgradePlan:
    """Plan eligible installed targets without downloading or activating them.

    Parameters
    ----------
    inventory : ManagerInventory
        Fresh manager inventory to evaluate.
    scopes : set[Scope]
        Configured scopes selected by the caller.
    check_target : callable
        Boundary that refreshes one target's ``update_status`` and returns an
        :class:`~gupkg.core.ActionResult`.

    Returns
    -------
    UpgradePlan
        User targets before system targets, each ``skipped`` (with a reason),
        ``failed`` (check failure), or ``eligible``.
    """
    complete = all(scope.complete for scope in inventory.scopes if scope.scope in scopes)
    selected = sorted(
        (target for target in inventory.targets if target.scope in scopes),
        key=lambda target: (_scope_order(target.scope), target.package.selector.casefold(), target.package.selector),
    )
    entries: list[UpgradePlanEntry] = []
    for target in selected:
        # Only healthy installed targets of complete scopes are ever checked.
        skip_reason = (
            "incomplete" if not complete
            else "uninstalled" if target.installation_status == "not-installed"
            else "broken" if target.installation_status == "broken"
            else "unhealthy" if target.health_status != "healthy"
            else None
        )
        if skip_reason is not None:
            entries.append(UpgradePlanEntry(target, "skipped", skip_reason))
            continue
        result = check_target(target)
        if target.update_status in {"not-configured", "current"} and result.ok:
            entries.append(UpgradePlanEntry(target, "skipped", target.update_status, result))
        elif target.update_status == "available" and result.ok:
            entries.append(UpgradePlanEntry(target, "eligible", result=result))
        else:
            entries.append(UpgradePlanEntry(target, "failed", "failed-check", result))
    return UpgradePlan(entries)


def execute_upgrade_plan(
    plan: UpgradePlan,
    revalidate_target,
    upgrade_target,
    *,
    cancel_requested=None,
) -> UpgradePlan:
    """Execute eligible plan entries in order and retain every outcome.

    A failing target never stops the batch; every later eligible target still
    runs.

    Parameters
    ----------
    plan : UpgradePlan
        Previously generated plan; its entries are updated in place.
    revalidate_target : callable
        Returns ``None`` when a target is still safe to mutate, or an
        explanatory string when it changed after planning.
    upgrade_target : callable
        Single-package operation returning an :class:`ActionResult`.
    cancel_requested : callable, optional
        Checked before each new package operation. A true result stops
        scheduling without interrupting an operation already in progress.

    Returns
    -------
    UpgradePlan
        The same plan, with ``upgraded``, ``current``, ``failed``, or
        ``not-attempted`` outcomes and results attached.
    """
    for entry in plan.entries:
        if entry.outcome != "eligible":
            continue
        if cancel_requested is not None and cancel_requested():
            entry.outcome, entry.reason = "not-attempted", "cancelled"
            continue

        # Revalidate ownership and health immediately before mutation, and
        # keep a failure as the entry's result so the earlier successful
        # check result cannot be reported for an invalidated target.
        try:
            problem = revalidate_target(entry.target)
            failure = ActionResult(False, errors=[problem], exit_code=EXIT_USER_ERROR) if problem else None
        except Exception as exc:
            failure = _exception_result(exc)
        if failure is not None:
            entry.result = failure
            entry.outcome, entry.reason = "failed", failure.errors[0]
            continue

        # Isolate one target's operational failure so later eligible targets
        # still run and the batch retains a result for every target.
        try:
            entry.result = upgrade_target(entry.target)
        except Exception as exc:
            entry.result = _exception_result(exc)
        if not entry.result.ok:
            entry.outcome, entry.reason = "failed", "upgrade-failed"
        elif entry.result.changed or entry.result.status in {"installed-update", "downloaded"}:
            entry.outcome = "upgraded"
        else:
            entry.outcome = "current"
    return plan


def _exception_result(exc: Exception) -> ActionResult:
    """Translate an exception from one per-target boundary into a failed result."""
    if isinstance(exc, (ConfigValidationError, ValueError, FileNotFoundError)):
        code = EXIT_USER_ERROR
    elif isinstance(exc, OSError):
        code = EXIT_MUTATION_ERROR
    else:
        code = EXIT_INTERNAL_ERROR
    return ActionResult(False, errors=[str(exc)], exit_code=code)


# ---------------------------------------------------------------------------
# Per-target operations
# ---------------------------------------------------------------------------


def manager_update_target(target: ManagedTarget, system: System) -> ActionResult:
    """Refresh one managed target's update status through the package workflow.

    Parameters
    ----------
    target : ManagedTarget
        Target whose active (or newest local) manifest should be checked; its
        ``update_status`` and ``candidate_version`` are updated.
    system : System
        Boundary for network, Git and junction reads.

    Returns
    -------
    ActionResult
        The update-check outcome and its process status.
    """
    from .gupkg import check_package_update

    wanted = target.installed_version or target.local_version
    manifest = next(
        (item for item in target.package.manifests if item.version_path.name == wanted), None
    )
    if manifest is None:
        result = ActionResult(False, errors=["No manifest is available for the local version"], exit_code=EXIT_USER_ERROR)
    else:
        result = _quiet(lambda: check_package_update(manifest.version_path, system))
    if result.ok and result.status in {"available", "current", "not-configured"}:
        target.update_status = result.status
    else:
        target.update_status = "error" if not result.ok else "not-configured"
    target.candidate_version = None
    if target.update_status == "available":
        from .updates import load_update_state, update_paths

        state = load_update_state(update_paths(target.package.root)["state"])
        target.candidate_version = state.get("lastCandidateVersion")
    target.checked_at = datetime.now(timezone.utc).isoformat()
    target.diagnostics.extend(result.errors)
    return result


def manager_revalidate_target(
    target: ManagedTarget, config: ManagerConfig, system: System
) -> str | None:
    """Recheck managed ownership and health immediately before mutation.

    Parameters
    ----------
    target : ManagedTarget
        Target whose activation and package health must still be valid.
    config : ManagerConfig
        Configuration whose scope root must still contain the target.
    system : System
        Boundary used to read the ``current`` junction.

    Returns
    -------
    str or None
        A diagnostic when revalidation fails, otherwise ``None``.
    """
    from .gupkg import health_check_package

    try:
        root = target.package.root.resolve()
        if not root.is_relative_to(config.root(target.scope).resolve()):
            return "target path escaped configured root"
        current = read_current(target.package.root, system)
        if current.status != "valid" or current.target is None:
            return f"current changed to {current.status}"
        if not current.target.resolve().is_relative_to(root):
            return "current target escaped configured root"
        if not _quiet(lambda: health_check_package(current.target, system, scope=target.scope)).ok:
            return "health changed before upgrade"
    except (OSError, ValueError, RuntimeError) as exc:
        return str(exc)
    return None


def manager_upgrade_target(
    target: ManagedTarget,
    config: ManagerConfig,
    system: System,
    *,
    no_checksum: bool = False,
    shim_linkage: str | None = None,
) -> ActionResult:
    """Check, stage, and activate one managed target's update.

    Parameters
    ----------
    target : ManagedTarget
        Managed package to update.
    config : ManagerConfig
        Manager configuration whose bin and package roots receive the install.
    system : System
        Boundary for every machine side effect of the update.
    no_checksum : bool, default=False
        Skip an applicable payload checksum.
    shim_linkage : {"dynamic", "static"}, optional
        Native wrapper linkage; defaults to the configured linkage.

    Returns
    -------
    ActionResult
        Structured package-update outcome.
    """
    from .gupkg import full_package_upgrade

    linkage = shim_linkage or config.shim_linkage
    return _quiet(lambda: full_package_upgrade(
        target.package.root,
        system,
        scope=target.scope,
        no_checksum=no_checksum,
        shim_linkage=linkage,
        install_context=installation_context(config, target.scope, system),
    ))


def manager_download_target(
    target: ManagedTarget,
    system: System,
    *,
    no_checksum: bool = False,
) -> ActionResult:
    """Stage one managed package update without activating it.

    The package workflow rechecks its source, so a staged payload is never
    inferred from stale manager inventory.

    Parameters
    ----------
    target : ManagedTarget
        Managed package whose configured update should be staged.
    system : System
        Boundary for network, Git and junction reads.
    no_checksum : bool, default=False
        Skip an applicable payload checksum while staging the update.

    Returns
    -------
    ActionResult
        Structured staging outcome.
    """
    from .gupkg import download_package_update

    return _quiet(lambda: download_package_update(
        target.package.root, system, no_checksum=no_checksum
    ))


def _quiet(operation):
    """Run a package workflow with its progress output captured and discarded."""
    with contextlib.redirect_stdout(io.StringIO()):
        return operation()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _exact_table_keys(table: object, required: set[str], context: str, *, optional: set[str] = frozenset()) -> None:
    """Require a TOML table to contain exactly the required and optional keys."""
    if not isinstance(table, dict):
        raise ConfigValidationError(f"{context} must be a table containing {', '.join(sorted(required))}")
    unknown = sorted(set(table) - required - optional)
    missing = sorted(required - set(table))
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown key(s): {', '.join(unknown)}")
        if missing:
            details.append(f"missing key(s): {', '.join(missing)}")
        raise ConfigValidationError(f"Invalid {context}: " + "; ".join(details))


def _expand_manager_path(value: object, base: Path, field_name: str) -> Path:
    """Expand ``%NAME%`` and ``~`` in one configured path and make it absolute."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"[{field_name}] must be a nonempty string")
    environment = {key.casefold(): item for key, item in os.environ.items()}

    def replace(match: re.Match[str]) -> str:
        """Substitute one case-insensitive environment variable."""
        name = match.group(1)
        if name.casefold() not in environment:
            raise ConfigValidationError(f"[{field_name}] references unresolved variable %{name}%")
        return environment[name.casefold()]

    path = Path(_VARIABLE_RE.sub(replace, value.strip())).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _inside_package_app(path: Path, root: Path) -> bool:
    """Return whether *path* is below ``<root>/[group/]<package>/<version>/App``."""
    if not path.is_relative_to(root):
        return False
    parts = path.relative_to(root).parts
    return any(part.casefold() == "app" and index >= 2 for index, part in enumerate(parts))


def _same_or_nested(left: Path, right: Path) -> bool:
    """Return whether two paths are equal or one contains the other."""
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)
