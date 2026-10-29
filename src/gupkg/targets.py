"""Resolve one target inside the scope of the run (design sections 6.2, 6.3 and 9.6).

A run has one ``Scope`` (its mode, root and settings), so resolving a target no longer chooses a mode: ``resolve_target``
finds the package or the distribution folder the target names inside the scope's root, checks the package's ``modes``
against the scope's mode before any work, and says what the install has to copy in. An elevated run is given only package
names and paths inside the admin-only system root; in a direct administrator shell also a distribution folder outside
the root, to be copied into it; it never reads a folder that standard users can change as if it were trusted (D31, D18).

Usage and API
-------------
Call ``resolve_target(TargetRequest(selector, ...), scope=..., system=..., env=...)``. The result carries the catalog's
resolution, warnings and, for a distribution folder, the folder to copy in. ``ModeRefused`` (from ``gupkg.model.scope``)
carries the message and exit code.

Implementation Approach
-----------------------
Paths are compared by identity (real path, case, ``..``, ``\\\\?\\`` prefix, trailing dots, junctions), so a junction
cannot move a package into another root. An elevated run checks the install record's folder before it trusts the record.

See Also
--------
gupkg.model.scope : the scope and the mode check.
gupkg.catalog.catalog : the catalog that resolves a selector.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .catalog.catalog import Catalog, Resolved, manifest_modes, parse_selector
from .catalog.local import DATA_DIR_NAME, canonical_path, is_inside, strip_extended_prefix
from .model.scope import ModeRefused, Scope, check_supported
from .model.version import MANIFEST_NAME
from .selfmgmt.elevation import UntrustedLocationError
from .system.port import Mode, System


@dataclass(frozen=True)
class TargetRequest:
    """One target and how the caller wants it treated.

    Attributes
    ----------
    selector : str
        ``name``, ``name@version``, ``registry/name`` or a path.
    operation : str, optional
        The command word (``install``, ``update`` ...), put into the refusal of a package that needs another mode.
    no_input : bool
        ``--no-input``: a question is not asked.
    """

    selector: str
    operation: str | None = None
    no_input: bool = False


@dataclass(frozen=True)
class ResolvedTarget:
    """The outcome of ``resolve_target``.

    ``source`` is set when the target is a distribution folder outside the root that has to be copied into ``scope.root``
    before the install (design 21.4); ``resolved`` is the catalog's view of the target, or ``None`` when it is not read
    (an elevated run never reads a folder that users can modify).
    """

    scope: Scope
    resolved: Resolved | None = None
    warnings: tuple[str, ...] = ()
    source: Path | None = None

    @property
    def settings(self):
        """Return the effective settings of the run."""
        return self.scope.settings


def resolve_target(request: TargetRequest, *, scope: Scope, system: System, env: Mapping[str, str]) -> ResolvedTarget:
    """Resolve one target inside *scope*.

    Parameters
    ----------
    request : TargetRequest
        The target and the options that affect it.
    scope : Scope
        The scope of the run.
    system : System
        The machine boundary (admin-only checks, junctions).
    env : mapping of str to str
        The environment used to expand configured paths.

    Returns
    -------
    ResolvedTarget
        The scope, the catalog resolution, warnings and the folder to copy in.

    Raises
    ------
    ModeRefused
        If the package does not support the scope's mode, or an elevated run is given a target it must not trust.
    gupkg.catalog.catalog.CatalogError
        If the target is not found, or a path is outside the root and is not a distribution folder.
    """
    elevated = scope.mode is Mode.SYSTEM
    catalog = Catalog(scope, system)
    parsed = parse_selector(request.selector, (entry.name for entry in scope.settings.registries))

    # A path is inside the scope's root, or a distribution folder outside it.
    outside = False
    target_folder: Path | None = None
    if parsed.kind == "path":
        target_folder = _normalize(parsed.path)
        outside = not _inside_scope(target_folder, scope)

    resolved: Resolved | None = None
    # A folder outside the root in an elevated run is data from a place users can modify: it is neither read nor trusted
    # here. A direct administrator shell may copy it into the system root (design 21.4).
    if not (outside and elevated):
        try:
            resolved = catalog.resolve_distribution(target_folder) if outside else catalog.resolve(request.selector)
        except UntrustedLocationError as exc:
            raise ModeRefused(f"{exc}. An elevated gupkg only acts on package names and paths inside the system root.") from exc
        view = resolved.view
        if elevated and view.record is not None and view.record.mode not in (None, Mode.SYSTEM):
            raise ModeRefused(
                f"Refusing {view.record.mode} mode in an elevated shell: an elevated gupkg never acts on a {view.record.mode} root "
                "(a standard user could use it to gain administrator rights). Run it from a normal (non-elevated) shell."
            )
        if view.local is not None and view.record is not None:
            try:
                scope.require_trusted(view.local.path / DATA_DIR_NAME, "the install record of this package")
            except UntrustedLocationError as exc:
                raise ModeRefused(
                    "Refusing the install record of this package: it lies in a folder that standard users can "
                    f"modify, and an elevated gupkg does not trust it ({exc}). Run it from a normal (non-elevated) shell."
                ) from exc

    # The package's own ``modes`` list, from the version that would be installed.
    supported = _supported_modes(resolved, target_folder if outside else None, elevated)
    if supported is not None:
        name = resolved.view.name if resolved is not None else (target_folder.parent.name if target_folder is not None else None)
        check_supported(scope, supported, command=request.operation, name=name)

    folder = _copy_source(target_folder) if outside else None
    return ResolvedTarget(scope, resolved, (), folder)


def _normalize(path: Path) -> Path:
    """Return *path* absolute, without ``..``, and without an extended-length prefix."""
    return Path(os.path.abspath(strip_extended_prefix(os.fspath(Path(path).expanduser()))))


def _inside_scope(path: Path, scope: Scope) -> bool:
    """Return whether *path* is the scope's root or inside it by identity."""
    return canonical_path(path) == canonical_path(scope.root) or is_inside(path, scope.root)


def _supported_modes(resolved: Resolved | None, folder: Path | None, elevated: bool) -> tuple[str, ...] | None:
    """Return the ``modes`` of the version that would be installed, or ``None`` when no version is known yet.

    An elevated run does not read a distribution folder before it is copied (the copy is checked instead).
    """
    if resolved is not None:
        view = resolved.view
        if view.local is not None and resolved.version is not None:
            version = view.local.version(resolved.version)
            if version is not None and version.manifest is not None:
                return version.manifest.modes
            return None
        if view.local is None and view.registry is not None and view.registry.versions:
            return manifest_modes(view.registry.versions[-1].path / MANIFEST_NAME)
        return None
    if folder is not None and not elevated:
        return manifest_modes(folder / MANIFEST_NAME)
    return None


def _copy_source(folder: Path | None) -> Path | None:
    """Return the distribution folder to copy into the root (design 21.4); it must be a version directory."""
    if folder is None:
        return None
    if (folder / MANIFEST_NAME).is_file():
        return folder
    raise ModeRefused(f"{folder} is outside every root and is not a version directory (no {MANIFEST_NAME}); name the version folder.")
