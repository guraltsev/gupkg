"""Derive the state of packages: the only place that joins roots, install records and registries.

``Catalog.describe`` builds one immutable ``PackageView``; ``Catalog.list`` lists every
package of every trusted root (and, on request, the registry entries that are not local);
``Catalog.resolve`` turns a selector of design 10.1 (``name``, ``name@version``,
``mode:name``, ``registry/name`` or a path) into a view; ``candidates``,
``install_candidate``, ``update_candidate``, ``tracker_sync`` and ``compare`` are the pure
decisions that installs, updates and ``check-updates`` take from a view. The catalog reads
disk and never changes it, prints nothing and asks nothing. A tracker is never run: only
versions already on disk or already in the linked registry are known (decision D11).

Usage and API
-------------
Build a catalog with ``Catalog.from_settings(settings, system, env)`` and call ``describe``,
``list`` and ``resolve``. ``resolve_package_path(path, system)`` resolves a path with no
settings, for callers that have only a ``System``. Errors are ``CatalogError`` subclasses
(``NotFound``, ``AmbiguousSelector``, ``NoVersionKnown``, ``NotInstalled``, ``NeedsRepair``,
``NoValidVersion``, ``UntrustedLocation``).

Implementation Approach
-----------------------
Roots come from the settings (the system root only, and only when admin-only, for an elevated
run, decision D31) plus the ad-hoc roots recorded in each root's ``state.toml``. Every package
is scanned by ``gupkg.catalog.local``, its install record is read by ``gupkg.catalog.record``,
and the registry entry it follows is joined from the cached registry snapshots: the entry with
its own name by default (decision D2), the entry of its explicit link, or none after an opt-out.
Status and candidates are then pure functions of the view (``gupkg.model.state``). Effects of
the install record are verified against the system only when asked, because verifying reads the
Windows environment.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..model.manifest import TrackResult
from ..model.settings import RegistryEntry as RegistryConfig
from ..model.settings import Settings, expand_env
from ..model.state import (
    MODES,
    Candidate,
    InstalledState,
    Link,
    PackageView,
    RegistryEntry,
    RegistryTracker,
    Root,
    known_candidates,
)
from ..model.version import MANIFEST_NAME, TRACKER_DIR_NAME, compare_versions, is_version_name
from ..system.port import Scope, System, is_elevated_run
from .local import (
    DATA_DIR_NAME,
    canonical_path,
    is_inside,
    is_reparse_point,
    looks_like_package,
    read_adhoc_roots,
    scan_package,
    scan_root,
)
from .record import read_install_record
from .registry import RegistrySnapshot, load_registry, registry_cache_dir


class CatalogError(ValueError):
    """Report that a selector or package cannot be used."""


class NotFound(CatalogError):
    """Report that no package, registry entry or version matches."""


class AmbiguousSelector(CatalogError):
    """Report that a bare name exists in several roots and a mutating command needs it qualified."""

    def __init__(self, name: str, matches: Sequence[str]) -> None:
        """Name the package and the qualified selectors that would be unambiguous."""
        self.name = name
        self.matches = tuple(matches)
        super().__init__(f"'{name}' exists in several roots; qualify it: {', '.join(self.matches)}")


class NoVersionKnown(CatalogError):
    """Report that a package has no version to install and only a tracker can find one."""

    def __init__(self, name: str) -> None:
        """Build the message of design 5.3 for package *name*."""
        self.name = name
        super().__init__(f"no version known yet; run `gupkg check-updates {name}`")


class NoValidVersion(CatalogError):
    """Report that a package has version directories but none of them is valid."""

    def __init__(self, name: str, problems: Sequence[str]) -> None:
        """Build the message from the problems of the invalid versions."""
        self.name = name
        super().__init__(f"'{name}' has no valid version; see the invalid version problems: {'; '.join(problems)}")


class NotInstalled(CatalogError):
    """Report that a command needs an installed package."""


class NeedsRepair(CatalogError):
    """Report that a broken package must be repaired before it can be updated."""


class UntrustedLocation(CatalogError):
    """Report that an elevated run refuses a location that standard users can modify."""


# ---------------------------------------------------------------------------
# Selectors and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selector:
    """A parsed selector: ``kind`` is ``name``, ``registry`` or ``path``."""

    text: str
    kind: str
    name: str | None = None
    mode: str | None = None
    version: str | None = None
    registry: str | None = None
    path: Path | None = None


@dataclass(frozen=True)
class Resolved:
    """A selector resolved to a package.

    ``version`` is the version the selector names (``name@version`` or a version directory),
    the active one for ``current`` or a package root with an active version, and otherwise the
    newest valid local version, or ``None``. ``is_tracker`` is set for a path to ``_check_updates``.
    """

    view: PackageView
    version: str | None = None
    via_current: bool = False
    is_tracker: bool = False
    named_version: bool = False

    @property
    def version_path(self) -> Path | None:
        """Return the directory of ``version`` in the package root, when there is one."""
        if self.view.local is None or self.version is None:
            return None
        state = self.view.local.version(self.version)
        return state.path if state is not None else None


@dataclass(frozen=True)
class ListFilter:
    """Filters of ``Catalog.list``; an empty filter lists every local package."""

    statuses: frozenset[str] | None = None
    modes: frozenset[str] | None = None
    query: str | None = None
    registry: str | None = None
    include_available: bool = False


@dataclass(frozen=True)
class Listing:
    """The views of ``Catalog.list`` and the warnings found on the way (shadowing, unreadable folders)."""

    views: tuple[PackageView, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrackerSync:
    """What to do with a linked package's tracker: ``none``, ``copy``, ``refresh`` or ``keep`` (edited locally)."""

    action: str
    reason: str
    registry_tracker: RegistryTracker | None = None


@dataclass(frozen=True)
class CheckPlan:
    """What ``check-updates`` does for a package: ``not-local``, ``nothing`` (no tracker) or ``run`` (run the tracker)."""

    action: str
    tracker: TrackerSync


@dataclass(frozen=True)
class Comparison:
    """How a tracker result relates to the known versions: ``newer``, ``known`` or ``older``."""

    status: str
    version: str
    newest_known: str | None = None
    reason: str = ""

    @property
    def newer(self) -> bool:
        """Return whether the result is newer than every known version."""
        return self.status == "newer"


_MODE_PREFIX = re.compile(r"^(system|user|portable):(?![\\/])(.+)$", re.IGNORECASE)
_DRIVE = re.compile(r"^[A-Za-z]:")


def parse_selector(text: str, registry_names: Iterable[str] = ()) -> Selector:
    r"""Parse a selector of design 10.1.

    Parameters
    ----------
    text : str
        ``name``, ``name@version``, ``mode:name``, ``registry/name`` or a path (anything
        containing ``\``, starting with ``.`` or ``/`` or with a drive).
    registry_names : iterable of str
        The configured registry names. A selector ``a/b`` whose first part is one of them
        selects a registry entry; otherwise ``a/b`` is a package inside the grouping folder ``a``.

    Returns
    -------
    Selector
        The parsed selector.

    Raises
    ------
    CatalogError
        If the selector is empty or names an unknown mode.
    """
    text = text.strip()
    if not text:
        raise CatalogError("empty selector")

    # A leading mode, as in ``user:firefox``, never a drive letter.
    mode: str | None = None
    rest = text
    match = _MODE_PREFIX.match(text)
    if match:
        mode, rest = match.group(1).lower(), match.group(2)

    # Anything that looks like a path is a path, whatever it names.
    if "\\" in rest or rest.startswith((".", "/")) or _DRIVE.match(rest):
        return Selector(text, "path", mode=mode, path=Path(rest))

    # An optional version, then the name or registry/name.
    name, version = rest, None
    if "@" in rest:
        name, _, version = rest.rpartition("@")
        if not name or not version:
            raise CatalogError(f"'{text}': expected name@version")
    if "/" in name:
        first, _, remainder = name.partition("/")
        if remainder and first.casefold() in {item.casefold() for item in registry_names}:
            return Selector(text, "registry", name=remainder, mode=mode, version=version, registry=first)
        if os.path.exists(text) and mode is None and version is None:
            return Selector(text, "path", path=Path(text))
    return Selector(text, "name", name=name, mode=mode, version=version)


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


@dataclass
class Catalog:
    """Read-only access to the packages of a set of roots and their registries.

    Parameters
    ----------
    system : System
        Boundary used to read ``current`` junctions, to ask whether the run is elevated,
        whether a folder is admin-only, and to verify effects.
    roots : sequence of Root
        The roots to scan (configured and ad-hoc).
    registries : sequence of RegistryConfig
        The configured registries, in priority order.
    data_roots : mapping
        Mode to the root whose data directory holds that mode's registry cache.
    elevated : bool, optional
        Whether the run is elevated; by default the system boundary decides.
    verify_effects : bool, default=False
        Whether ``describe`` compares the install record's effects with the system (it reads
        the environment and shortcuts; ``check`` turns it on, ``list`` leaves it off).
    """

    system: System
    roots: tuple[Root, ...] = ()
    registries: tuple[RegistryConfig, ...] = ()
    data_roots: Mapping[str, Path] = field(default_factory=dict)
    elevated: bool | None = None
    verify_effects: bool = False
    warnings: list[str] = field(default_factory=list)
    _snapshots: dict[tuple[str, str], RegistrySnapshot] = field(default_factory=dict, repr=False)
    _trusted: tuple[Root, ...] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Fix the elevation of the run once."""
        if self.elevated is None:
            self.elevated = is_elevated_run(self.system)

    @classmethod
    def from_settings(
        cls, settings: Settings, system: System, env: Mapping[str, str], *, elevated: bool | None = None,
        verify_effects: bool = False,
    ) -> "Catalog":
        """Build the catalog of a run from its effective settings.

        Parameters
        ----------
        settings : Settings
            The merged settings (roots and registries).
        system : System
            The machine boundary.
        env : mapping
            Environment used to expand ``%NAME%`` in the configured roots.
        elevated : bool, optional
            Overrides the elevation reported by *system*.
        verify_effects : bool, default=False
            See the class.

        Returns
        -------
        Catalog
            A catalog whose roots are the configured ones (only the system root when
            elevated, decision D31) and the ad-hoc roots recorded in their ``state.toml``.
        """
        is_elevated = is_elevated_run(system) if elevated is None else elevated
        configured = {mode: Path(expand_env(settings[f"roots.{mode}"], env)) for mode in MODES}
        warnings: list[str] = []
        roots: list[Root] = []
        for mode, path in configured.items():
            if is_elevated and mode != "system":
                continue
            roots.append(Root(path, mode))

            # Ad-hoc roots recorded in this root's data directory (design 5.4). An elevated run
            # reads that file only when the data directory is admin-only.
            reason = system.directory_untrusted_reason(path / DATA_DIR_NAME) if is_elevated else None
            if reason is not None:
                warnings.append(f"Did not read {path / DATA_DIR_NAME / 'state.toml'} in an elevated run: {reason}")
                continue
            adhoc, problem = read_adhoc_roots(path / DATA_DIR_NAME)
            if problem:
                warnings.append(problem)
            for entry in adhoc:
                if is_elevated and entry.mode != "system":
                    continue
                target = Path(entry.path)
                roots.append(Root(target, entry.mode, adhoc=True, single=looks_like_package(target)))
        catalog = cls(system, tuple(roots), tuple(settings.registries), configured, is_elevated, verify_effects)
        catalog.warnings.extend(warnings)
        return catalog

    # -- describing ---------------------------------------------------------

    def describe(self, name: str, root: Root | None = None) -> PackageView:
        """Build the view of one package.

        Parameters
        ----------
        name : str
            The package name (``group/name`` for a package inside grouping folders).
        root : Root, optional
            The root to look in. Without one, every trusted root is searched and the first
            match wins; with no local package at all, a registry entry gives an ``available`` view.

        Returns
        -------
        PackageView
            The immutable view.

        Raises
        ------
        NotFound
            If neither a local package nor a registry entry has that name.
        """
        if root is not None:
            local_view = self._local_view(root, name)
            if local_view is not None:
                return local_view
        else:
            for candidate_root in self._trusted_roots():
                local_view = self._local_view(candidate_root, name)
                if local_view is not None:
                    return local_view
        entry = self._first_entry(name, root.mode if root is not None else None)
        if entry is None:
            raise NotFound(f"package '{name}' was not found in any root or registry")
        return PackageView(name, root, None, registry=entry)

    def list(self, filters: ListFilter | None = None) -> Listing:
        """List the packages of every trusted root, with warnings.

        Parameters
        ----------
        filters : ListFilter, optional
            Status, mode, name and registry filters; ``include_available`` adds registry
            entries that are not local in any root (``root`` is ``None`` for those).

        Returns
        -------
        Listing
            Views sorted by name and mode, and warnings: folders that could not be read,
            and a "shadowing" warning for a name that exists in both the system and the
            user root (their shims shadow each other on ``PATH``, decision D21).
        """
        filters = filters or ListFilter()
        views: list[PackageView] = []
        roots = self._trusted_roots()
        warnings = list(self.warnings)
        seen: set[str] = set()
        for root in roots:
            scan = scan_root(root)
            warnings.extend(scan.diagnostics)
            for package, path in scan.packages:
                # A package that an ad-hoc entry names again inside a configured root is listed once.
                if canonical_path(path) not in seen:
                    seen.add(canonical_path(path))
                    views.append(self._view(root, package, path))

        # The same name in the system and the user root is reported once.
        by_name: dict[str, set[str]] = {}
        for view in views:
            by_name.setdefault(view.name.casefold(), set()).add(view.mode or "")
        for key, modes in sorted(by_name.items()):
            if {"system", "user"} <= modes:
                name = next(view.name for view in views if view.name.casefold() == key)
                warnings.append(
                    f"shadowing: '{name}' exists in both the system and the user root; "
                    "their shims on PATH shadow each other"
                )

        # Registry entries with no local package anywhere are ``available``.
        if filters.include_available:
            local_names = set(by_name)
            for snapshot in self._snapshots_for(None):
                for entry in snapshot.entries.values():
                    if entry.name.casefold() not in local_names:
                        local_names.add(entry.name.casefold())
                        views.append(PackageView(entry.name, None, None, registry=entry))

        views = [view for view in views if _matches(view, filters)]
        views.sort(key=lambda view: (view.name.casefold(), MODES.index(view.mode) if view.mode in MODES else len(MODES)))
        return Listing(tuple(views), tuple(warnings))

    # -- selecting ----------------------------------------------------------

    def resolve(self, selector: str, for_mutation: bool = False) -> Resolved:
        """Resolve a selector of design 10.1 to a package.

        Parameters
        ----------
        selector : str
            ``name``, ``name@version``, ``mode:name``, ``registry/name`` or a path.
        for_mutation : bool, default=False
            Whether the caller changes the package. A bare name that exists in several roots
            is then ambiguous and must be qualified with its mode.

        Returns
        -------
        Resolved
            The view and the version the selector names (see ``Resolved``).

        Raises
        ------
        NotFound
            If nothing matches, or a path is not a package.
        AmbiguousSelector
            If a bare name exists in several roots and *for_mutation* is set.
        UntrustedLocation
            If an elevated run is given a path in a folder that standard users can modify.
        """
        parsed = parse_selector(selector, (item.name for item in self.registries))
        if parsed.kind == "path":
            return self._resolve_path(parsed)

        # ``registry/name`` selects that registry's entry, whether or not a local package exists.
        if parsed.kind == "registry":
            snapshot = next((item for item in self._snapshots_for(parsed.mode) if item.name.casefold() == parsed.registry.casefold()), None)
            entry = snapshot.entry(parsed.name) if snapshot is not None else None
            if entry is None:
                raise NotFound(f"registry entry '{parsed.registry}/{parsed.name}' was not found")
            matches = self._matches_for(parsed.name, parsed.mode)
            if len(matches) > 1 and for_mutation:
                raise AmbiguousSelector(parsed.name, _qualified(parsed.name, matches))
            if matches:
                view = self._view(matches[0][0], matches[0][1], matches[0][2], follow=(entry.registry, entry.name))
            else:
                view = PackageView(entry.name, self._root_for_mode(parsed.mode), None, registry=entry)
            return Resolved(view, parsed.version, named_version=parsed.version is not None)

        # A bare name: the roots decide, then the registries.
        matches = self._matches_for(parsed.name, parsed.mode)
        if len(matches) > 1 and for_mutation:
            raise AmbiguousSelector(parsed.name, _qualified(parsed.name, matches))
        if matches:
            view = self._view(*matches[0])
        else:
            entry = self._first_entry(parsed.name, parsed.mode)
            if entry is None:
                raise NotFound(f"package '{parsed.name}' was not found in any root or registry")
            view = PackageView(entry.name, self._root_for_mode(parsed.mode), None, registry=entry)
        return Resolved(view, parsed.version, named_version=parsed.version is not None)

    # -- decisions ------------------------------------------------------------

    def candidates(self, view: PackageView) -> list[Candidate]:
        """Return the versions an install or update may choose from, newest first.

        Pure: nothing is read and no tracker is run (decision D11). See ``known_candidates``.
        """
        return list(known_candidates(view))

    def install_candidate(self, view: PackageView) -> Candidate:
        """Choose the version that ``install`` activates (design 5.3).

        Parameters
        ----------
        view : PackageView
            The package. For one whose ``current`` is valid, ``install`` repairs: it chooses
            the active version.

        Returns
        -------
        Candidate
            For a package that is not installed, the newest valid local version directory;
            with none, the newest pinned version of the followed registry entry (it is copied
            in). ``_check_updates`` is never chosen.

        Raises
        ------
        NoVersionKnown
            If there is no version at all (a package with only a tracker): the caller reports
            ``no version known yet; run `gupkg check-updates <name>``` and makes no request.
        NoValidVersion
            If the package has version directories and every one of them is invalid.
        """
        if view.installed is not None:
            return Candidate(view.installed.version, "local", view.installed.path)
        found = self.candidates(view)
        local = [item for item in found if item.source == "local"]
        if local:
            return local[0]
        if found:
            return found[0]
        if view.local is not None and view.local.versions:
            raise NoValidVersion(
                view.name, [f"{item.name}: {', '.join(item.problems)}" for item in view.local.versions if item.problems]
            )
        raise NoVersionKnown(view.name)

    def update_candidate(self, view: PackageView) -> Candidate | None:
        """Choose the version that ``update`` activates for an installed package.

        Returns
        -------
        Candidate or None
            The newest of the valid local versions and the followed registry's pinned versions
            that is newer than ``current``, or ``None`` when the package is up to date.

        Raises
        ------
        NotInstalled
            If the package is not installed (decision D9).
        NeedsRepair
            If the package is broken; the hint names ``check --fix`` and ``install``.
        """
        if view.broken_reasons:
            raise NeedsRepair(
                f"'{view.name}' is broken ({'; '.join(view.broken_reasons)}); "
                f"run `gupkg check --fix` or `gupkg install {view.name}`"
            )
        if view.installed is None:
            raise NotInstalled(f"'{view.name}' is not installed; use `gupkg install {view.name}`")
        found = self.candidates(view)
        return found[0] if found else None

    def pick_version(self, view: PackageView, version: str) -> Candidate:
        """Return the candidate that ``name@version`` names, local first, among every known valid version.

        Raises
        ------
        NotFound
            If neither the package root nor the followed registry holds a valid such version.
        """
        folded = version.casefold()
        if view.local is not None:
            state = view.local.version(version)
            if state is not None and state.valid:
                return Candidate(state.name, "local", state.path)
        if view.registry is not None:
            pinned = view.registry.version(version)
            if pinned is not None and (view.local is None or view.local.version(version) is None):
                return Candidate(pinned.name, "registry", pinned.path, view.registry.registry, pinned.fingerprint)
        known = sorted({item.name for item in (view.local.valid_versions if view.local else ())}
                       | {item.name for item in (view.registry.versions if view.registry else ())})
        raise NotFound(f"'{view.name}' has no valid version {folded}; known versions: {', '.join(known) or 'none'}")

    def tracker_sync(self, view: PackageView) -> TrackerSync:
        """Decide whether the registry's tracker is copied in, refreshed or kept (design 5.3 rule 2).

        Returns
        -------
        TrackerSync
            ``copy`` when the package follows an entry with a tracker and has none; ``refresh``
            when the local copy still has the hash recorded at copy time and the registry's differs;
            ``keep`` when the local tracker was edited or never came from the registry; ``none`` otherwise.
        """
        if view.registry is None or view.registry.tracker is None:
            return TrackerSync("none", "the followed registry has no tracker")
        registry_tracker = view.registry.tracker
        local = view.local.tracker if view.local is not None else None
        if local is None:
            return TrackerSync("copy", "the package has no tracker", registry_tracker)
        recorded = view.link.tracker_sha256 if view.link is not None else None
        if recorded is None:
            return TrackerSync("keep", "the local tracker was not copied from the registry")
        if local.sha256 != recorded:
            return TrackerSync("keep", "the local tracker was edited since it was copied")
        if registry_tracker.sha256 == recorded:
            return TrackerSync("none", "the local tracker is up to date")
        return TrackerSync("refresh", "the registry's tracker changed and the local copy is unedited", registry_tracker)

    def check_updates_plan(self, view: PackageView) -> CheckPlan:
        """Decide what ``check-updates`` does for a package (design 5.3, third column).

        Returns
        -------
        CheckPlan
            ``not-local`` for a registry entry with no local package (the caller hints ``install``);
            ``run`` when the package has a tracker, or one that the linked registry's tracker would
            supply (copied in first, ``tracker`` says how); ``nothing`` otherwise. Pure: no tracker runs here.
        """
        if view.local is None:
            return CheckPlan("not-local", self.tracker_sync(view))
        sync = self.tracker_sync(view)
        has_tracker = view.local.tracker is not None or sync.action in ("copy", "refresh")
        return CheckPlan("run" if has_tracker else "nothing", sync)

    def compare(self, view: PackageView, result: TrackResult) -> Comparison:
        """Decide whether a tracker result is newer than every known version (decision D21).

        Parameters
        ----------
        view : PackageView
            The package that was checked.
        result : TrackResult
            What its tracker found.

        Returns
        -------
        Comparison
            ``newer`` (write a new version directory), ``known`` (a directory or registry version
            of that name exists) or ``older``. Every local directory counts as known, valid or
            not, because a new directory must never overwrite one.
        """
        version = result.version
        if not is_version_name(version):
            return Comparison("older", version, reason=f"'{version}' cannot name a version directory")
        known: list[str] = []
        if view.local is not None:
            known.extend(item.name for item in view.local.versions)
        if view.registry is not None:
            known.extend(item.name for item in view.registry.versions)
        newest = None
        for name in known:
            if newest is None or compare_versions(name, newest) > 0:
                newest = name
        if any(name.casefold() == version.casefold() for name in known):
            return Comparison("known", version, newest, f"version {version} is already known")
        if newest is not None and compare_versions(version, newest) <= 0:
            return Comparison("older", version, newest, f"version {version} is not newer than {newest}")
        return Comparison("newer", version, newest, "newer than every known version")

    # -- internals ------------------------------------------------------------

    def _trusted_roots(self) -> tuple[Root, ...]:
        """Return the roots that may be read; an elevated run reads only admin-only system roots."""
        if self._trusted is not None:
            return self._trusted
        configured_first = tuple(sorted(self.roots, key=lambda root: root.adhoc))
        if not self.elevated:
            self._trusted = configured_first
            return self._trusted

        # An elevated run never reads a folder that standard users can modify (design 9.4, D31).
        usable: list[Root] = []
        for root in configured_first:
            reason = self.system.directory_untrusted_reason(root.path)
            if reason is None and root.mode == "system":
                usable.append(root)
            else:
                self.warnings.append(f"Skipped {root.path} in an elevated run: " + (reason or "not the system root"))
        self._trusted = tuple(usable)
        return self._trusted

    def _root_for_mode(self, mode: str | None) -> Root | None:
        """Return the trusted root of *mode*, or ``None`` when no mode is named or no root has it."""
        return next((root for root in self._trusted_roots() if root.mode == mode and not root.adhoc), None)

    def _matches_for(self, name: str, mode: str | None) -> list[tuple[Root, str, Path]]:
        """Return ``(root, name, path)`` for every trusted root that holds a local package called *name*."""
        found: list[tuple[Root, str, Path]] = []
        seen: set[str] = set()
        for root in self._trusted_roots():
            if mode is not None and root.mode != mode:
                continue
            path = _package_path(root, name)
            if path is not None and _is_local_package(path) and canonical_path(path) not in seen:
                seen.add(canonical_path(path))
                found.append((root, name, path))
        return found

    def _local_view(self, root: Root, name: str) -> PackageView | None:
        """Return the view of *name* in *root*, or ``None`` when the root has no such package."""
        if self.elevated and root.path not in [item.path for item in self._trusted_roots()]:
            return None
        path = _package_path(root, name)
        if path is None or not _is_local_package(path):
            return None
        return self._view(root, name, path)

    def _view(self, root: Root, name: str, path: Path, follow: tuple[str, str] | None = None) -> PackageView:
        """Build the view of the package root *path*; *follow* overrides which registry entry it follows."""
        local = scan_package(path, self.system)
        read = read_install_record(path)
        record = read.record
        problems: list[str] = []
        warnings: list[str] = []
        if read.problem:
            problems.append(read.problem)
        if self.elevated and record is not None and record.mode not in (None, "system"):
            problems.append(f"the install record says mode {record.mode}; an elevated run acts only on system packages")
        if record is not None and record.mode is not None and root.mode is not None and record.mode != root.mode:
            problems.append(f"the install record says mode {record.mode}, but the package lies in a {root.mode} root")

        # Join the registry entry the package follows (decision D2).
        link = record.link if record is not None else None
        entry, unfollowed, link_warning = self._follow(name, record.mode if record and record.mode else root.mode, link, follow)
        if link_warning:
            warnings.append(link_warning)

        # Installed: a valid ``current`` whose active App is checked.
        installed = None
        if local.current.status == "valid":
            active = local.version(local.current.version)
            populated = active.populated if active is not None else False
            effects = record.effects if record is not None else ()
            drift, unverified = self._verify(effects) if self.verify_effects else ((), ())
            installed = InstalledState(
                local.current.version, local.current.target, populated,
                None if populated else f"the App of {local.current.version} is missing or empty",
                effects, drift, unverified,
            )

        # A local directory that differs from the registry's copy is never overwritten: it is diverged.
        diverged: tuple[str, ...] = ()
        if entry is not None:
            diverged = tuple(
                item.name for item in local.versions
                if (pinned := entry.version(item.name)) is not None and pinned.pkg_toml_sha256 != item.pkg_toml_sha256
            )
        return PackageView(
            name, root, local, entry, unfollowed, link, installed, diverged, tuple(problems), tuple(warnings), record
        )

    def _snapshots_for(self, mode: str | None) -> list[RegistrySnapshot]:
        """Return the registry snapshots to join for packages of *mode*, in configured order.

        A system run (or package) uses only the system root's cache; other modes use the
        first of their own, the user and the system root's cache that has been synchronised.
        """
        preference = {"system": ("system",), "user": ("user", "system"), "portable": ("portable", "user", "system"), None: ("user", "system")}
        order = ("system",) if self.elevated else preference.get(mode, ("user", "system"))
        base = next(
            (self.data_roots[item] for item in order if item in self.data_roots and any(
                (registry_cache_dir(self.data_roots[item], config.name) / "state.toml").is_file() for config in self.registries
            )),
            None,
        )
        if base is None:
            return []
        snapshots = []
        for config in self.registries:
            cache = registry_cache_dir(base, config.name)
            key = (str(cache), config.name)
            if key not in self._snapshots:
                self._snapshots[key] = load_registry(config.name, cache, self.system)
            snapshots.append(self._snapshots[key])
        return snapshots

    def _first_entry(self, name: str, mode: str | None) -> RegistryEntry | None:
        """Return the entry called *name* in the first registry that has it (design 17), or ``None``."""
        for snapshot in self._snapshots_for(mode):
            entry = snapshot.entry(name)
            if entry is not None:
                return entry
        return None

    def _follow(
        self, name: str, mode: str | None, link: Link | None, override: tuple[str, str] | None
    ) -> tuple[RegistryEntry | None, RegistryEntry | None, str | None]:
        """Return ``(followed entry, entry not followed, warning)`` for a local package."""
        if override is None and link is not None and link.unlinked:
            return None, self._first_entry(name, mode), None

        # An explicit link (or the override of ``registry/name``) names a registry and an entry.
        registry, target = override if override is not None else (link.registry if link else None, link.name if link else None)
        target = target or name
        if registry is None:
            return self._first_entry(target, mode), None, None
        snapshot = next((item for item in self._snapshots_for(mode) if item.name.casefold() == registry.casefold()), None)
        entry = snapshot.entry(target) if snapshot is not None else None
        if entry is None:
            return None, None, f"the link to {registry}/{target} cannot be followed: the entry is not in the registry cache"
        return entry, None, None

    def _verify(self, effects: Sequence) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Compare recorded effects with the system; return ``(drift, unverified kinds)``."""
        drift: list[str] = []
        unverified: list[str] = []
        for effect in effects:
            fields = effect.fields
            scope = {"system": Scope.MACHINE, "user": Scope.USER}.get(fields.get("scope", ""))
            try:
                if effect.kind in ("shim", "file"):
                    if not os.path.exists(str(fields["path"])):
                        drift.append(f"{effect.kind} missing: {fields['path']}")
                elif effect.kind == "shortcut":
                    try:
                        self.system.read_shortcut(Path(str(fields["path"])))
                    except OSError:
                        drift.append(f"shortcut missing: {fields['path']}")
                elif effect.kind == "env" and scope is not None and not (self.elevated and scope is Scope.USER):
                    stored = self.system.get_env(scope, str(fields["name"]))
                    if stored is None or stored.value != str(fields["value"]):
                        drift.append(f"environment variable {fields['name']} differs")
                elif effect.kind == "path" and scope is not None and not (self.elevated and scope is Scope.USER):
                    entries = [item.casefold().rstrip("\\") for item in self.system.get_path(scope)]
                    if str(fields["entry"]).casefold().rstrip("\\") not in entries:
                        drift.append(f"PATH entry missing: {fields['entry']}")
                else:
                    unverified.append(effect.kind)
            except KeyError as exc:
                drift.append(f"malformed {effect.kind} effect: no {exc.args[0]}")
        return tuple(drift), tuple(unverified)

    def _resolve_path(self, parsed: Selector) -> Resolved:
        """Resolve a path selector: a package root, a version directory, ``_check_updates`` or ``current``."""
        # Normalise lexically so a trailing ``current`` is not followed before it is classified.
        candidate = Path(os.path.abspath(os.fspath(parsed.path.expanduser())))
        version: str | None = None
        via_current = False
        is_tracker = False
        if candidate.name.casefold() == "current":
            package_root = candidate.parent
            via_current = True
        elif candidate.is_dir() and (candidate / MANIFEST_NAME).is_file():
            package_root = candidate.parent
            if candidate.name == TRACKER_DIR_NAME:
                is_tracker = True
            elif is_version_name(candidate.name):
                version = candidate.name
            else:
                raise NotFound(f"{candidate} is not a version directory: its name is reserved or not a safe folder name")
        elif candidate.is_dir():
            package_root = candidate
        else:
            raise NotFound(f"Path does not exist: {candidate}")
        if self.elevated:
            self._require_system_path(package_root, parsed.mode)
        if not _is_local_package(package_root, allow_link=not self.elevated):
            raise NotFound(f"Not a package root, version directory or current: {candidate}")

        # The root decides the mode: the configured root that contains the path, else the record, else auto.
        root, name = self._root_of(package_root, parsed.mode)
        view = self._view(root, name, package_root)
        if via_current:
            if view.local.current.status != "valid":
                raise NotFound(view.local.current.problem or f'"current" path does not exist: {candidate}')
            version = view.local.current.version
        elif version is None and not is_tracker:
            if view.installed is not None:
                version, via_current = view.installed.version, True
            else:
                valid = view.local.valid_versions
                version = valid[-1].name if valid else None
        return Resolved(view, version, via_current, is_tracker)

    def _require_system_path(self, package_root: Path, mode: str | None) -> None:
        """Refuse, in an elevated run, a path that is not in the system root or a trusted ad-hoc root (D31).

        A catalog with no roots (``resolve_package_path``) can only ask whether the folder is admin-only.
        """
        if mode not in (None, "system"):
            raise UntrustedLocation(f"Refusing mode {mode} in an elevated run: an elevated gupkg acts only on system packages")
        reason = self.system.directory_untrusted_reason(package_root)
        if reason is not None:
            raise UntrustedLocation(f"Refusing {package_root} in an elevated run: {reason}")
        if self.roots and not any(
            (root.single and canonical_path(root.path) == canonical_path(package_root))
            or (not root.single and is_inside(package_root, root.path))
            for root in self._trusted_roots()
        ):
            raise UntrustedLocation(f"Refusing {package_root} in an elevated run: it is not in the system root")

    def _root_of(self, package_root: Path, mode: str | None) -> tuple[Root, str]:
        """Return the root that holds *package_root* and the package's name in it."""
        for root in self._trusted_roots():
            if root.single and canonical_path(root.path) == canonical_path(package_root):
                return replace(root, mode=mode or root.mode), package_root.name
            if not root.single and is_inside(package_root, root.path):
                relative = os.path.relpath(os.path.realpath(package_root), os.path.realpath(root.path))
                return replace(root, mode=mode or root.mode), relative.replace("\\", "/")

        # No configured root contains it: an ad-hoc root, whose mode the install record may decide.
        read = read_install_record(package_root)
        recorded = read.record.mode if read.record is not None else None
        return Root(package_root.parent, mode or recorded, adhoc=True), package_root.name


def resolve_package_path(path: Path | str, system: System) -> Resolved:
    """Resolve a path to a package with no settings, for callers that have only a ``System``.

    Parameters
    ----------
    path : Path or str
        A package root, a version directory, ``_check_updates`` or ``current``.
    system : System
        Boundary used to read ``current`` and to refuse untrusted locations in an elevated run.

    Returns
    -------
    Resolved
        The package view (with no registry) and the selected version.

    Raises
    ------
    NotFound
        If the path is not a package.
    UntrustedLocation
        If an elevated run is given a folder that standard users can modify.
    """
    return Catalog(system)._resolve_path(Selector(str(path), "path", path=Path(path)))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _package_path(root: Root, name: str) -> Path | None:
    """Return the package root of *name* in *root*, or ``None`` for a name that could leave the root."""
    if root.single:
        return root.path if root.path.name.casefold() == name.casefold() else None
    parts = name.split("/")
    if any(not part or part in (".", "..") or part.startswith(".") or ":" in part or "\\" in part for part in parts):
        return None
    return root.path.joinpath(*parts)


def _is_local_package(path: Path, *, allow_link: bool = False) -> bool:
    """Return whether *path* is a package root; a link is accepted only for an explicit path (*allow_link*)."""
    try:
        return path.is_dir() and (allow_link or not is_reparse_point(path)) and looks_like_package(path)
    except OSError:
        return False


def _qualified(name: str, matches: Sequence[tuple[Root, str, Path]]) -> list[str]:
    """Return the ``mode:name`` selectors that tell the matches apart."""
    return [f"{root.mode or 'path'}:{name}" for root, _, _ in matches]


def _matches(view: PackageView, filters: ListFilter) -> bool:
    """Return whether *view* passes *filters*."""
    if filters.statuses is not None and view.status not in filters.statuses:
        return False
    if filters.modes is not None and view.mode not in filters.modes:
        return False
    if filters.query is not None and filters.query.casefold() not in view.name.casefold():
        return False
    if filters.registry is not None and (view.registry is None or view.registry.registry.casefold() != filters.registry.casefold()):
        return False
    return True
