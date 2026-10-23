"""Resolve package layouts and maintain the active-version junction.

User paths may identify a version directory, a package root, or its ``current``
junction. Resolution always produces one directory-derived package identity,
and junction updates refuse unsafe targets outside the owning package root.

Usage and API
-------------
``identity_from_path(...)`` turns a user path into a :class:`PackageIdentity`
for the 0.17 commands (the catalog, ``gupkg.catalog``, derives package state),
and ``update_current_junction_if_needed(...)`` activates a version. The destinations of a scope come from the
``ModeContext`` (``gupkg.modes.scope_context``).

Implementation Approach
-----------------------
Paths are classified lexically so a trailing ``current`` component is not
dereferenced prematurely. Activation prepares and verifies a temporary
junction before atomically replacing the active path, with rollback for
interrupted replacements.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Tuple

from .catalog.catalog import CatalogError, NotFound, resolve_package_path
from .catalog.local import read_current, strip_extended_prefix
from .core import (
    BOOTSTRAP_DIR_NAME,
    PackageIdentity,
    compare_package_versions,
    is_package_directory,
    log_info,
    log_warning,
    normalize_path,
)
from .system.port import System


def identity_from_path(raw_path: Path, system: System) -> Tuple[PackageIdentity, bool]:
    """Resolve a user-supplied path to one concrete package version for the 0.17 commands.

    A thin adapter over the catalog (``resolve_package_path`` and ``read_current``) that the
    0.17 engine uses until issue 029 replaces its front end. It also accepts 0.17's
    ``_bootstrap`` template folder, which the catalog does not know (decision D33).

    Parameters
    ----------
    raw_path : Path
        A version directory, a ``current`` junction, or a package root.
    system : System
        Boundary used to read ``current`` junctions.

    Returns
    -------
    Tuple[PackageIdentity, bool]
        ``(identity, installing_from_current)``, where the flag reports that the caller
        named ``current`` or a package root with a valid ``current``. A package root with no
        ``current`` resolves to its newest version directory, however many it holds.

    Raises
    ------
    ValueError
        If the path does not match a supported package layout, or ``current`` is unusable.
    """
    # Normalize lexically so a trailing ``current`` component is classified before it is followed.
    candidate = Path(os.path.abspath(strip_extended_prefix(os.fspath(Path(raw_path).expanduser()))))
    if candidate.name == BOOTSTRAP_DIR_NAME:
        package_root = candidate.parent
        active = read_current(package_root, system).target
        return PackageIdentity.from_version_path(
            package_root, candidate, is_current=active is not None and normalize_path(active) == normalize_path(candidate)
        ), False

    try:
        resolved = resolve_package_path(candidate, system)
    except NotFound as exc:
        template = candidate / BOOTSTRAP_DIR_NAME
        if candidate.name.casefold() != "current" and template.is_dir() and is_package_directory(template):
            return PackageIdentity.from_version_path(candidate, template, is_current=False), False
        if candidate.name.casefold() == "current":
            raise ValueError(f'"current" path is not usable: {exc}') from exc
        raise ValueError(str(exc)) from exc
    except CatalogError as exc:
        raise ValueError(str(exc)) from exc
    if resolved.is_tracker:
        raise ValueError(f"{candidate} is the update tracker, not a version directory")

    # ``current`` is never reinterpreted: naming it, or the package root, with a broken one is an error and a
    # valid one names the version. An explicit version directory stays usable so a damaged ``current`` can be repaired.
    local = resolved.view.local
    package_root = local.path
    if local.current.status not in ("absent", "valid") and (resolved.via_current or candidate == package_root):
        raise ValueError(f'"current" path is not a valid junction: {local.current.problem}')
    if resolved.via_current:
        return PackageIdentity.from_version_path(package_root, local.current.target, is_current=True), True
    if candidate != package_root:
        version = local.version(candidate.name)
        return PackageIdentity.from_version_path(
            package_root, candidate, is_current=version is not None and local.current.version == version.name
        ), False

    # A package root that is not installed resolves to its newest version directory.
    if not local.versions:
        raise ValueError(f"Package root has no version directory to use: {package_root}")
    return PackageIdentity.from_version_path(package_root, local.versions[-1].path, is_current=False), False


def installed_version(package_root: Path, system: System) -> str | None:
    """Return the active version of a package root for the 0.17 commands, from its catalog ``PackageView``.

    Parameters
    ----------
    package_root : Path
        The package root.
    system : System
        Boundary used to read ``current``.

    Returns
    -------
    str or None
        The version ``current`` activates, or ``None`` when the package is not installed or ``current`` is unusable.
    """
    try:
        view = resolve_package_path(package_root, system).view
    except CatalogError:
        return None
    return view.installed.version if view.installed is not None else None


def update_current_junction_if_needed(
    identity: PackageIdentity, system: System, *, allow_downgrade: bool = False
) -> bool:
    r"""Point ``<package>\current`` at *identity* unless a newer version should win.

    Re-activating the version that is already active still recreates the
    junction: install reruns are the supported repair path.

    Parameters
    ----------
    identity : PackageIdentity
        Package version that should become or remain ``current``.
    system : System
        Boundary that creates, reads and removes the junctions.
    allow_downgrade : bool
        Whether to replace ``current`` when it already points to a newer
        version.

    Returns
    -------
    bool
        ``True`` when ``current`` was recreated or repointed; ``False`` only
        when it was left untouched because a newer version is active.

    Raises
    ------
    ValueError
        If the existing ``current`` path is unsafe or malformed.
    RuntimeError
        If the junction replacement fails.

    """
    current_path = identity.package_root / "current"
    desired_target = identity.version_path
    if not desired_target.is_dir():
        raise RuntimeError(f"Junction target does not exist or is not a directory: {desired_target}")

    # Inspect the existing activation: it must be a junction inside this
    # package root, and a newer active version wins without --allow-downgrade.
    if os.path.lexists(current_path):
        if not system.is_link(current_path):
            raise ValueError(f"{current_path} exists but is not a junction. Aborting all operations.")
        current_target = system.read_link(current_path)
        if current_target is None:
            raise ValueError(f"{current_path} is a junction but its target is not resolvable. Aborting.")
        current_target = current_target.resolve()
        if not current_target.is_dir():
            log_info(f"JUNCTION: stale current target detected: {current_target}")
        elif current_target.parent != identity.package_root.resolve():
            raise ValueError(
                f"{current_path} is a junction but its target {current_target} "
                f"is not under {identity.package_root}. Aborting."
            )
        else:
            log_info(f"'current' junction version: {current_target.name}")
            if compare_package_versions(identity.version_string, current_target.name) < 0:
                if not allow_downgrade:
                    log_info(f"JUNCTION: keeping current ({current_target.name} > {identity.version_string})")
                    return False
                log_info(f"JUNCTION: --allow-downgrade: updating current to {identity.version_string}")

    # Build and verify a temporary junction, then swap it in. The old
    # junction is parked under a unique name so a failed swap can restore it.
    suffix = uuid.uuid4().hex[:8]
    new_path = current_path.with_name(f"current.__new__.{suffix}")
    old_path = current_path.with_name(f"current.__old__.{suffix}")
    try:
        system.create_junction(new_path, desired_target)
        new_target = system.read_link(new_path)
        if new_target is None or normalize_path(new_target) != normalize_path(desired_target):
            raise RuntimeError(
                f"Temporary junction verification failed: expected {desired_target}, got {new_target}"
            )
        if os.path.lexists(current_path):
            os.replace(current_path, old_path)
        os.replace(new_path, current_path)
        if os.path.lexists(old_path):
            system.remove_link(old_path)
    finally:
        if os.path.lexists(new_path):
            try:
                system.remove_link(new_path)
            except (OSError, ValueError):
                pass
        if os.path.lexists(old_path) and not os.path.lexists(current_path):
            try:
                os.replace(old_path, current_path)
            except OSError:
                pass
    log_info(f"JUNCTION: created: {current_path.name} -> {desired_target}")
    return True


def warn_if_output_path_is_unusual(
    kind: str, default_root: Path, expanded_name: str, final_path: Path
) -> None:
    """Warn when a shortcut or wrapper output lands outside its default root.

    Relative nested paths inside the default root are silent. Absolute names
    and escaping ``..`` traversal remain allowed, but are called out.

    Parameters
    ----------
    kind : str
        ``shortcut`` or ``bin``, used in the warning.
    default_root : Path
        The scope's default output root.
    expanded_name : str
        The configured name after variable expansion.
    final_path : Path
        The output path that will be written.
    """
    looks_absolute = expanded_name.startswith(("/", "\\")) or re.match(
        r"^[A-Za-z]:[\\/]", expanded_name
    ) is not None

    # Walk the relative segments; going above the root at any point escapes it
    # even when later segments come back down.
    depth = 0
    escapes = False
    for segment in re.split(r"[\\/]+", expanded_name):
        if segment in ("", "."):
            continue
        depth += -1 if segment == ".." else 1
        if depth < 0:
            escapes = True
            break
    try:
        escapes = escapes or not final_path.resolve(strict=False).is_relative_to(
            default_root.resolve(strict=False)
        )
    except OSError:
        escapes = True
    if looks_absolute or escapes:
        destination = expanded_name if looks_absolute else str(final_path)
        log_warning(
            f"{kind} output resolves outside the default {kind} root; this is allowed but unusual: {destination}"
        )
