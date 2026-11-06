"""Derive the state of packages: the only place that joins roots, install records and registries.

``Catalog.describe`` builds one immutable ``PackageView``; ``Catalog.list`` lists every
package of the scope's one root (and, on request, the registry entries that are not local);
``Catalog.resolve`` turns a selector of design 10.1 (``name``, ``name@version``,
``registry/name`` or a path) into a view; ``candidates``,
``install_candidate``, ``update_candidate``, ``tracker_sync`` and ``compare`` are the pure
decisions that installs, updates and ``check-updates`` take from a view. The catalog reads
disk and never changes it, prints nothing and asks nothing. A tracker is never run: only
versions already on disk or already in the linked registry are known (decision D11).

Usage and API
-------------
Build a catalog with ``Catalog(scope, system)`` and call ``describe``, ``list`` and ``resolve``.
``resolve_distribution(path)`` reads a distribution folder outside the root, to be copied in. Errors are
``CatalogError`` subclasses (``NotFound``, ``NoVersionKnown``, ``NotInstalled``, ``NeedsRepair``,
``NoValidVersion``; a location that an elevated run must not trust raises ``UntrustedLocationError``).

Implementation Approach
-----------------------
The catalog reads one root, ``scope.root``, and joins one registry cache, ``<root>\\.gupkg\\registry``.
A package in any other folder is not seen, and a path outside the root is refused unless it is a distribution
folder to be copied in. Every package is scanned by ``gupkg.catalog.local``, its install record is loaded by ``gupkg.catalog.record``,
and the registry entry it follows is joined from the cached registry snapshots: the entry with
its own name by default (decision D2), the entry of its explicit link, or none after an opt-out.
Status and candidates are then pure functions of the view (``gupkg.model.state``). Effects of
the install record are verified against the system only when asked, because verifying reads the
Windows environment.
"""

from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..model.manifest import ManifestError, TrackResult, invalid_checksum_problem, parse_manifest
from ..model.settings import RegistryEntry as RegistryConfig
from ..model.state import (
    Candidate,
    Effect,
    InstalledState,
    Link,
    PackageView,
    RegistryEntry,
    RegistryTracker,
    RegistryVersion,
    Root,
    VersionState,
    known_candidates,
)
from ..model.version import MANIFEST_NAME, TRACKER_DIR_NAME, compare_versions, is_version_name
from ..model.plan import normalize_entry
from ..model.scope import ModeRefused, Scope, check_supported
from ..selfmgmt.elevation import UntrustedLocationError
from ..system.port import Mode, System
from .local import (
    is_inside,
    is_link_info,
    is_reparse_point,
    looks_like_package,
    real_path,
    scan_package,
    scan_root,
    strip_extended_prefix,
)
from .record import read_install_record, record_path
from .registry import RegistrySnapshot, load_registry, registry_cache_dir


class CatalogError(ValueError):
    """Report that a selector or package cannot be used."""


class NotFound(CatalogError):
    """Report that no package, registry entry or version matches."""


class NoVersionKnown(CatalogError):
    """Report that a package has no version to install and only a tracker can find one.

    Parameters
    ----------
    name : str
        The package that has no version.
    message : str, optional
        What to say instead of the message of design 5.3.
    """

    def __init__(self, name: str, message: str | None = None) -> None:
        """Build the message of design 5.3 for package *name*, unless *message* says more."""
        self.name = name
        super().__init__(message or f"no version known yet; run `gupkg check-updates {name}`")


class NoValidVersion(CatalogError):
    """Report that a package has version directories but none of them is valid.

    Parameters
    ----------
    name : str
        The package whose versions are all invalid.
    problems : Sequence[str]
        The problems of the invalid versions, by version.
    """

    def __init__(self, name: str, problems: Sequence[str]) -> None:
        """Build the message from the problems of the invalid versions."""
        self.name = name
        super().__init__(f"'{name}' has no valid version; see the invalid version problems: {'; '.join(problems)}")
        # A malformed declared hash has its own stable error code (the manager reads the code, never the prose).
        for problem in problems:
            if invalid_checksum_problem([problem]):
                self.error_code = "invalid-checksum"
                self.error_details = {"version": problem.split(":", 1)[0], "field": "source.sha256"}
                break


class NotInstalled(CatalogError):
    """Report that a command needs an installed package."""


class NeedsRepair(CatalogError):
    """Report that a broken package must be repaired before it can be updated."""


# ---------------------------------------------------------------------------
# Selectors and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selector:
    """A parsed selector: ``kind`` is ``name``, ``registry`` or ``path``.

    Attributes
    ----------
    text : str
        The selector as the user wrote it.
    kind : str
        ``name``, ``registry`` or ``path``.
    name : str | None
        The package name, for a ``name`` or ``registry`` selector.
    version : str | None
        The version after ``@``, when the selector names one.
    registry : str | None
        The registry named by a ``registry/name`` selector.
    path : Path | None
        The folder named by a ``path`` selector.
    """

    text: str
    kind: str
    name: str | None = None
    version: str | None = None
    registry: str | None = None
    path: Path | None = None


@dataclass(frozen=True)
class Resolved:
    """A selector resolved to a package.

    ``version`` is the version the selector names (``name@version`` or a version directory),
    the active one for ``current`` or a package root with an active version, and otherwise the
    newest valid local version, or ``None``. ``is_tracker`` is set for a path to ``_check_updates``.

    Attributes
    ----------
    view : PackageView
        The view of the package the selector names.
    version : str | None
        The version the selector selects, or ``None``.
    via_current : bool
        Whether the selector named ``current`` or a package root with an active version.
    is_tracker : bool
        Whether the selector is a path to ``_check_updates``.
    named_version : bool
        Whether the selector named a version explicitly.
    version_path : Path | None
        Return the directory of ``version`` in the package root, when there is one.
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
    """Filters of ``Catalog.list``; an empty filter lists every local package.

    Attributes
    ----------
    statuses : frozenset[str] | None
        The statuses to keep, or ``None`` for every status.
    query : str | None
        A text that the package name must contain, or ``None``.
    registry : str | None
        Keep only the packages of this registry, or ``None``.
    include_available : bool
        Whether packages found only in a registry are listed.
    """

    statuses: frozenset[str] | None = None
    query: str | None = None
    registry: str | None = None
    include_available: bool = False


@dataclass(frozen=True)
class Listing:
    """The views of ``Catalog.list`` and the warnings found on the way (unreadable folders).

    Attributes
    ----------
    views : tuple[PackageView, ...]
        The package views that passed the filter.
    warnings : tuple[str, ...]
        Problems found while scanning, such as unreadable folders.
    """

    views: tuple[PackageView, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrackerSync:
    """What to do with a linked package's tracker: ``none``, ``copy``, ``refresh`` or ``keep`` (edited locally).

    Attributes
    ----------
    action : str
        ``none``, ``copy``, ``refresh`` or ``keep``.
    reason : str
        Why that action was chosen.
    registry_tracker : RegistryTracker | None
        The registry's tracker the action refers to, if any.
    """

    action: str
    reason: str
    registry_tracker: RegistryTracker | None = None


@dataclass(frozen=True)
class CheckPlan:
    """What ``check-updates`` does for a package: ``not-local``, ``nothing`` (no tracker) or ``run`` (run the tracker).

    Attributes
    ----------
    action : str
        ``not-local``, ``nothing`` or ``run``.
    tracker : TrackerSync
        The tracker synchronisation that goes with the action.
    """

    action: str
    tracker: TrackerSync


@dataclass(frozen=True)
class Comparison:
    """How a tracker result relates to the known versions: ``newer``, ``known`` or ``older``.

    Attributes
    ----------
    status : str
        ``newer``, ``known`` or ``older``.
    version : str
        The version the tracker found.
    newest_known : str | None
        The newest version already known, or ``None``.
    reason : str
        A readable explanation of the status.
    newer : bool
        Return whether the result is newer than every known version.
    """

    status: str
    version: str
    newest_known: str | None = None
    reason: str = ""

    @property
    def newer(self) -> bool:
        """Return whether the result is newer than every known version."""
        return self.status == "newer"


def parse_selector(text: str, registry_names: Iterable[str] = ()) -> Selector:
    r"""Parse a selector of design 10.1.

    Parameters
    ----------
    text : str
        ``name``, ``name@version``, ``registry/name`` or a path (anything
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
        If the selector is empty, or has a ``mode:`` prefix (``system:name``, ``user:name``): a run has one scope,
        so the mode is never part of a target.

    Examples
    --------
    Basic usage:

    >>> selector = parse_selector("tool@1.2")
    >>> selector.kind, selector.name, selector.version
    ('name', 'tool', '1.2')

    A registry prefix is recognised only for a configured registry:

    >>> selector = parse_selector("main/tool", registry_names=["main"])
    >>> selector.kind, selector.registry, selector.name
    ('registry', 'main', 'tool')

    Paths:

    >>> parse_selector("C:/Tools/tool").kind
    'path'
    >>> parse_selector("./tool").path.name
    'tool'

    Edge cases:

    >>> parse_selector("tool@")
    Traceback (most recent call last):
        ...
    gupkg.catalog.catalog.CatalogError: 'tool@': expected name@version
    >>> parse_selector("system:tool")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    gupkg.catalog.catalog.CatalogError: 'system:tool': a target no longer names a mode; ...
    """
    # Reject empty selectors and the removed ``system:``/``user:`` prefixes before classifying the text.
    removed_prefix = re.compile(r"^(system|user):(?![\\/])", re.IGNORECASE)
    text = text.strip()
    if not text:
        raise CatalogError("empty selector")
    rest = text
    if removed_prefix.match(text):
        raise CatalogError(
            f"'{text}': a target no longer names a mode; a run works in the mode of its own token. "
            "Run gupkg from an administrator shell for system packages, otherwise for user packages."
        )

    # Anything that looks like a path is a path, whatever it names.
    if "\\" in rest or rest.startswith((".", "/")) or re.match(r"^[A-Za-z]:", rest):
        return Selector(text, "path", path=Path(rest))

    # An optional version, then the name or registry/name.
    name, version = rest, None
    if "@" in rest:
        name, _, version = rest.rpartition("@")
        if not name or not version:
            raise CatalogError(f"'{text}': expected name@version")

    # ``registry/name`` when the first part is a configured registry; otherwise a grouped package or an existing path.
    if "/" in name:
        first, _, remainder = name.partition("/")
        if remainder and first.casefold() in {item.casefold() for item in registry_names}:
            return Selector(text, "registry", name=remainder, version=version, registry=first)
        if os.path.exists(text) and version is None:
            return Selector(text, "path", path=Path(text))
    return Selector(text, "name", name=name, version=version)


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


@dataclass
class Catalog:
    """Read-only access to the packages of one root and its registry cache.

    Parameters
    ----------
    scope : Scope
        The scope of the run: its mode, its root (the only folder scanned) and its settings (the registries, in
        priority order). The registry cache is ``<root>\\.gupkg\\registry``.
    system : System
        Boundary used to read ``current`` junctions, to ask whether a folder is admin-only, and to verify effects.
    verify_effects : bool, default=False
        Whether ``describe`` compares the install record's effects with the system (it reads
        the environment and shortcuts; ``check`` turns it on, ``list`` leaves it off).

    Methods
    -------
    describe
        Build the view of one package.
    list
        List the packages of the root, with warnings.
    installed_effects
        Return the recorded effects of every package of the root that has any, by package name.
    resolve
        Resolve a selector of design 10.1 to a package.
    resolve_distribution
        Resolve a distribution folder outside the root (a version directory with a ``pkg.toml``), to
        be copied in.
    registry_entry
        Return the entry called *name* in the first registry that has it, or ``None``.
    candidates
        Return the versions an install or update may choose from, newest first.
    install_candidate
        Choose the version that ``install`` activates (design 5.3).
    update_candidate
        Choose the version that ``update`` activates for an installed package.
    pick_version
        Return the candidate that ``name@version`` names, local first, among every known valid
        version.
    tracker_sync
        Decide whether the registry's tracker is copied in, refreshed or kept (design 5.3 rule 2).
    check_updates_plan
        Decide what ``check-updates`` does for a package (design 5.3, third column).
    compare
        Decide whether a tracker result is newer than every known version (decision D21).
    """

    scope: Scope
    system: System
    verify_effects: bool = False
    warnings: list[str] = field(default_factory=list)
    _snapshots: dict[tuple[str, str], RegistrySnapshot] = field(default_factory=dict, repr=False)
    _untrusted: tuple[str | None] | None = field(default=None, repr=False)

    @property
    def root(self) -> Root:
        """Return the one root this catalog reads."""
        return Root(self.scope.root)

    def _untrusted_reason(self) -> str | None:
        """Return why an elevated run must not read the root (standard users can modify it), or ``None`` (design 9.4, D31)."""
        if self._untrusted is None:
            try:
                self.scope.require_trusted(self.scope.root, "the root")
                reason = None
            except UntrustedLocationError as error:
                reason = str(error)
            self._untrusted = (reason,)
        return self._untrusted[0]

    @property
    def registries(self) -> tuple[RegistryConfig, ...]:
        """Return the configured registries, in priority order."""
        return tuple(self.scope.settings.registries)

    # -- describing ---------------------------------------------------------

    def describe(self, name: str) -> PackageView:
        """Build the view of one package.

        Parameters
        ----------
        name : str
            The package name (``group/name`` for a package inside grouping folders).

        Returns
        -------
        PackageView
            The immutable view of the package in the root; with no local package, a registry entry gives an
            ``available`` view.

        Raises
        ------
        NotFound
            If neither a local package nor a registry entry has that name.
        """
        local_view = self._local_view(name)
        if local_view is not None:
            return local_view
        entry = self._first_entry(name)
        if entry is None:
            raise NotFound(f"package '{name}' was not found in the root or any registry")
        return PackageView(name, None, None, registry=entry)

    def list(self, filters: ListFilter | None = None) -> Listing:
        """List the packages of the root, with warnings.

        Parameters
        ----------
        filters : ListFilter, optional
            Status, name and registry filters; ``include_available`` adds registry entries that are not local
            (``root`` is ``None`` for those; one the run's mode cannot install carries a ``restriction``).

        Returns
        -------
        Listing
            Views sorted by name, and warnings: folders that could not be read.
        """
        # An untrusted root is skipped in an elevated run, with the reason as a warning.
        filters = filters or ListFilter()
        reason = self._untrusted_reason()
        if reason is not None:
            return Listing((), (*self.warnings, f"Skipped {self.scope.root} in an elevated run: {reason}"))

        # One view per local package.
        scan = scan_root(self.root)
        views = [self._view(package, path) for package, path in scan.packages]

        # Registry entries with no local package are ``available``.
        if filters.include_available:
            local_names = {view.name.casefold() for view in views}
            for snapshot in self._snapshots_for():
                for entry in snapshot.entries.values():
                    if entry.name.casefold() not in local_names:
                        # Mark entries this mode cannot install, so the manager can show why.
                        local_names.add(entry.name.casefold())
                        restriction = self._restriction(entry)
                        views.append(PackageView(
                            entry.name, None, None, registry=entry,
                            restriction=restriction[0] if restriction else None,
                            restriction_reason=restriction[1] if restriction else None,
                        ))

        # Apply the filters, then order by name.
        views = [view for view in views if _matches(view, filters)]
        views.sort(key=lambda view: view.name.casefold())
        return Listing(tuple(views), (*self.warnings, *scan.diagnostics))

    def installed_effects(self) -> dict[str, tuple[Effect, ...]]:
        """Return the recorded effects of every package of the root that has any, by package name.

        This is who provides which shim, wrapper, shortcut and environment variable (name ownership): nothing outside the
        run's root is consulted. Only the folder names and the install records are loaded (no version scan, no registry join), and a
        record that cannot be loaded provides nothing here (``check`` and ``list`` report it).

        Returns
        -------
        dict
            Maps each package name to the tuple of its recorded ``Effect`` values.
        """
        # An untrusted root provides nothing to an elevated run.
        if self._untrusted_reason() is not None:
            return {}

        # One cached record lookup per package; packages without effects are left out.
        found: dict[str, tuple[Effect, ...]] = {}
        for name, path in scan_root(self.root).packages:
            effects = _record_effects(path)
            if effects:
                found[name] = effects
        return found

    # -- selecting ----------------------------------------------------------

    def resolve(self, selector: str) -> Resolved:
        """Resolve a selector of design 10.1 to a package.

        Parameters
        ----------
        selector : str
            ``name``, ``name@version``, ``registry/name`` or a path inside the root.

        Returns
        -------
        Resolved
            The view and the version the selector names (see ``Resolved``).

        Raises
        ------
        NotFound
            If nothing matches, a path is not a package, or a path is not inside the root.
        gupkg.selfmgmt.elevation.UntrustedLocationError
            If an elevated run is given a path in a folder that standard users can modify.
        """
        # Parse first; a path selector has its own resolution rules.
        parsed = parse_selector(selector, (item.name for item in self.registries))
        if parsed.kind == "path":
            return self._resolve_path(parsed)

        # ``registry/name`` selects that registry's entry, whether or not a local package exists.
        if parsed.kind == "registry":
            snapshot = next((item for item in self._snapshots_for() if item.name.casefold() == parsed.registry.casefold()), None)
            entry = snapshot.entry(parsed.name) if snapshot is not None else None
            if entry is None:
                raise NotFound(f"registry entry '{parsed.registry}/{parsed.name}' was not found")

            # A local package of that name is shown following the chosen entry.
            path = self._local_path(parsed.name)
            if path is not None:
                view = self._view(parsed.name, path, follow=(entry.registry, entry.name))
            else:
                view = PackageView(entry.name, None, None, registry=entry)
            return Resolved(view, parsed.version, named_version=parsed.version is not None)

        # A bare name: the root decides, then the registries.
        path = self._local_path(parsed.name)
        if path is not None:
            view = self._view(parsed.name, path)
        else:
            entry = self._first_entry(parsed.name)
            if entry is None:
                raise NotFound(f"package '{parsed.name}' was not found in the root or any registry")
            view = PackageView(entry.name, None, None, registry=entry)
        return Resolved(view, parsed.version, named_version=parsed.version is not None)

    def resolve_distribution(self, folder: Path) -> Resolved:
        """Resolve a distribution folder outside the root (a version directory with a ``pkg.toml``), to be copied in.

        Parameters
        ----------
        folder : Path
            The distribution folder.

        Returns
        -------
        Resolved
            The view of the package and the version the folder holds.

        Raises
        ------
        NotFound
            If *folder* is not such a folder (design 21.4).
        """
        return self._resolve_path(Selector(str(folder), "path", path=Path(folder)), distribution=True)

    def registry_entry(self, name: str) -> RegistryEntry | None:
        """Return the entry called *name* in the first registry that has it, or ``None``.

        Used for a distribution folder that is not a package of the root yet: the registry may publish the
        fingerprint that verifies it (design 9.2).

        Parameters
        ----------
        name : str
            The package name to look up.

        Returns
        -------
        RegistryEntry or None
            The entry of the first configured registry that has the package.
        """
        return self._first_entry(name)

    # -- decisions ------------------------------------------------------------

    def candidates(self, view: PackageView) -> list[Candidate]:
        """Return the versions an install or update may choose from, newest first.

        Pure: no disk is consulted and no tracker is run (decision D11). See ``known_candidates``.

        Parameters
        ----------
        view : PackageView
            The package.

        Returns
        -------
        list of Candidate
            Valid local versions and the followed registry's pinned versions, newest first.
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
        # An installed package with a valid ``current`` repairs the active version.
        if view.installed is not None:
            return Candidate(view.installed.version, "local", view.installed.path)

        # Otherwise prefer a local directory, then the registry's newest pinned version.
        found = self.candidates(view)
        local = [item for item in found if item.source == "local"]
        if local:
            return local[0]
        if found:
            return found[0]

        # Nothing usable: say whether versions exist but are all invalid, or none is known.
        if view.local is not None and view.local.versions:
            raise NoValidVersion(
                view.name, [f"{item.name}: {', '.join(item.problems)}" for item in view.local.versions if item.problems]
            )
        raise NoVersionKnown(view.name)

    def update_candidate(self, view: PackageView) -> Candidate | None:
        """Choose the version that ``update`` activates for an installed package.

        Parameters
        ----------
        view : PackageView
            The installed package.

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
        # Only a healthy installed package can be updated.
        if view.broken_reasons:
            raise NeedsRepair(
                f"'{view.name}' is broken ({'; '.join(view.broken_reasons)}); "
                f"run `gupkg check --fix` or `gupkg install {view.name}`"
            )
        if view.installed is None:
            raise NotInstalled(f"'{view.name}' is not installed; use `gupkg install {view.name}`")

        # Candidates are newest first, so the first one is the update, if any.
        found = self.candidates(view)
        return found[0] if found else None

    def pick_version(self, view: PackageView, version: str) -> Candidate:
        """Return the candidate that ``name@version`` names, local first, among every known valid version.

        Parameters
        ----------
        view : PackageView
            The package.
        version : str
            The version name asked for.

        Returns
        -------
        Candidate
            The local version directory when it is valid, otherwise the registry's pinned version.

        Raises
        ------
        NotFound
            If neither the package root nor the followed registry holds a valid such version.
        """
        # A valid local directory wins; a registry version counts only when no local directory of that name exists.
        folded = version.casefold()
        if view.local is not None:
            state = view.local.version(version)
            if state is not None and state.valid:
                return Candidate(state.name, "local", state.path)
        if view.registry is not None:
            pinned = view.registry.version(version)
            if pinned is not None and (view.local is None or view.local.version(version) is None):
                return Candidate(pinned.name, "registry", pinned.path, view.registry.registry, pinned.fingerprint)

        # Nothing matched: list the known versions in the error.
        known = sorted({item.name for item in (view.local.valid_versions if view.local else ())}
                       | {item.name for item in (view.registry.versions if view.registry else ())})
        raise NotFound(f"'{view.name}' has no valid version {folded}; known versions: {', '.join(known) or 'none'}")

    def tracker_sync(self, view: PackageView) -> TrackerSync:
        """Decide whether the registry's tracker is copied in, refreshed or kept (design 5.3 rule 2).

        Parameters
        ----------
        view : PackageView
            The package.

        Returns
        -------
        TrackerSync
            ``copy`` when the package follows an entry with a tracker and has none; ``refresh``
            when the local copy still has the hash recorded at copy time and the registry's differs;
            ``keep`` when the local tracker was edited or never came from the registry; ``none`` otherwise.
        """
        # Without a registry tracker there is nothing to copy; without a local one it is copied in.
        if view.registry is None or view.registry.tracker is None:
            return TrackerSync("none", "the followed registry has no tracker")
        registry_tracker = view.registry.tracker
        local = view.local.tracker if view.local is not None else None
        if local is None:
            return TrackerSync("copy", "the package has no tracker", registry_tracker)

        # The hash recorded at copy time tells whether the local copy was edited.
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

        Parameters
        ----------
        view : PackageView
            The package.

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
        """Decide whether a tracker result is newer than every local version directory (decision D21).

        Parameters
        ----------
        view : PackageView
            The package that was checked.
        result : TrackResult
            What its tracker found.

        Returns
        -------
        Comparison
            ``newer`` (write a new version directory), ``known`` (a directory of that name exists)
            or ``older``. Every local directory counts as known, valid or not, because a new
            directory must never overwrite one. The registry is a source of its own
            (``registry_update``) and takes no part here.
        """
        # A version that cannot name a directory is never newer.
        version = result.version
        if not is_version_name(version):
            return Comparison("older", version, reason=f"'{version}' cannot name a version directory")

        # Every local directory counts as known.
        known = [item.name for item in view.local.versions] if view.local is not None else []

        # Find the newest known version, then classify the result against it.
        newest = None
        for name in known:
            if newest is None or compare_versions(name, newest) > 0:
                newest = name
        if any(name.casefold() == version.casefold() for name in known):
            return Comparison("known", version, newest, f"version {version} is already known")
        if newest is not None and compare_versions(version, newest) <= 0:
            return Comparison("older", version, newest, f"version {version} is not newer than {newest}")
        return Comparison("newer", version, newest, "newer than every known version")

    def registry_update(self, view: PackageView) -> RegistryVersion | None:
        """Return the newest pinned version of the followed registry entry when it is newer than every local directory.

        Parameters
        ----------
        view : PackageView
            The package that is checked.

        Returns
        -------
        RegistryVersion or None
            The version the registry offers, or ``None`` when it has nothing newer.
        """
        if view.registry is None or not view.registry.versions:
            return None
        newest = view.registry.versions[-1]
        local = view.local.versions if view.local is not None else ()
        return None if any(compare_versions(item.name, newest.name) >= 0 for item in local) else newest

    def unfinished_version(self, view: PackageView) -> VersionState | None:
        """Return the newest local version when it is valid, has no ``App`` and has something to populate one from.

        Parameters
        ----------
        view : PackageView
            The package that is checked.

        Returns
        -------
        VersionState or None
            The version a failed, refused or skipped download left without its ``App``, or ``None``. A version that ships its
            ``App`` has no download to repeat, and only the newest version counts: an older one is not on the way to ``update``.
        """
        newest = None
        for item in view.local.versions if view.local is not None else ():
            if newest is None or compare_versions(item.name, newest.name) > 0:
                newest = item
        if newest is None or newest.populated or newest.manifest is None or newest.problems:
            return None
        return None if newest.manifest.source.populate == "none" else newest

    # -- internals ------------------------------------------------------------

    def _local_path(self, name: str) -> Path | None:
        """Return the package root of the local package called *name* in the root, or ``None``."""
        path = _package_path(self.scope.root, name)
        if path is None or self._untrusted_reason() is not None:
            return None
        return path if _is_local_package(path) else None

    def _local_view(self, name: str) -> PackageView | None:
        """Return the view of *name* in the root, or ``None`` when the root has no such package."""
        path = self._local_path(name)
        return self._view(name, path) if path is not None else None

    def _view(self, name: str, path: Path, follow: tuple[str, str] | None = None) -> PackageView:
        """Build the view of the package root *path*; *follow* overrides which registry entry it follows."""
        # Scan the folder and load its install record; record problems become view problems.
        local = scan_package(path, self.system)
        read = read_install_record(path)
        record = read.record
        problems: list[str] = []
        warnings: list[str] = []
        if read.problem:
            problems.append(read.problem)
        elif record is None and local.current.status != "absent":
            problems.append(f"no install record (made by an older gupkg); reinstall with: gupkg install {name}")

        # A record made in the other scope means the package was moved.
        if record is not None and record.mode is not None and record.mode != self.scope.mode:
            problems.append(f"the install record says {record.mode}; this package was moved between scopes — reinstall it")

        # Join the registry entry the package follows (decision D2).
        link = record.link if record is not None else None
        entry, unfollowed, link_warning = self._follow(name, link, follow)
        if link_warning:
            warnings.append(link_warning)

        # Installed: a valid ``current`` whose active App is checked.
        installed = None
        if local.current.status == "valid":
            active = local.version(local.current.version)
            populated = active.populated if active is not None else False

            # Effects are compared with the system only when verification was requested.
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

        # Assemble the immutable view from everything joined above.
        return PackageView(
            name, self.root, local, entry, unfollowed, link, installed, diverged, tuple(problems), tuple(warnings), record
        )

    def _snapshots_for(self) -> list[RegistrySnapshot]:
        """Return the registry snapshots of the root's own cache, in configured order (none before the first sync)."""
        # Before the first sync (or in an untrusted root) there is nothing to load.
        if self._untrusted_reason() is not None or not any(
            (registry_cache_dir(self.scope.root, config.name) / "state.toml").is_file() for config in self.registries
        ):
            return []

        # Load each configured registry once per catalog and keep them in priority order.
        snapshots = []
        for config in self.registries:
            cache = registry_cache_dir(self.scope.root, config.name)
            key = (str(cache), config.name)
            if key not in self._snapshots:
                self._snapshots[key] = load_registry(config.name, cache, self.scope)
            snapshots.append(self._snapshots[key])
        return snapshots

    def _first_entry(self, name: str) -> RegistryEntry | None:
        """Return the entry called *name* in the first registry that has it (design 17), or ``None``."""
        for snapshot in self._snapshots_for():
            entry = snapshot.entry(name)
            if entry is not None:
                return entry
        return None

    def _restriction(self, entry: RegistryEntry) -> tuple[str, str] | None:
        """Return ``(label, refusal text)`` when the newest pinned version of *entry* cannot be installed in this mode (Q14)."""
        modes = manifest_modes(entry.versions[-1].path / MANIFEST_NAME) if entry.versions else None
        if modes is None:
            return None
        try:
            check_supported(self.scope, modes, command="install", name=entry.name)
        except ModeRefused as refusal:
            return ("system only" if self.scope.mode is Mode.USER else "user only"), str(refusal)
        return None

    def _follow(
        self, name: str, link: Link | None, override: tuple[str, str] | None
    ) -> tuple[RegistryEntry | None, RegistryEntry | None, str | None]:
        """Return ``(followed entry, entry not followed, warning)`` for a local package."""
        if override is None and link is not None and link.unlinked:
            return None, self._first_entry(name), None

        # An explicit link (or the override of ``registry/name``) names a registry and an entry.
        registry, target = override if override is not None else (link.registry if link else None, link.name if link else None)
        target = target or name
        if registry is None:
            return self._first_entry(target), None, None
        snapshot = next((item for item in self._snapshots_for() if item.name.casefold() == registry.casefold()), None)
        entry = snapshot.entry(target) if snapshot is not None else None
        if entry is None:
            return None, None, f"the link to {registry}/{target} cannot be followed: the entry is not in the registry cache"
        return entry, None, None

    def _verify(self, effects: Sequence) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Compare recorded effects with the system; return ``(drift, unverified kinds)``."""
        # Each effect kind has its own check; unknown kinds are reported as unverified, not as drift.
        drift: list[str] = []
        unverified: list[str] = []
        for effect in effects:
            fields = effect.fields
            try:
                # Shims and files must exist; shortcuts must still be readable.
                if effect.kind in ("shim", "file"):
                    if not os.path.exists(str(fields["path"])):
                        drift.append(f"{effect.kind} missing: {fields['path']}")
                elif effect.kind == "shortcut":
                    try:
                        self.system.read_shortcut(Path(str(fields["path"])))
                    except OSError:
                        drift.append(f"shortcut missing: {fields['path']}")
                # Environment variables are compared through the system boundary.
                elif effect.kind == "env":
                    stored = self.system.get_env(self.scope.mode, str(fields["name"]))
                    if stored is None or stored.value != str(fields["value"]):
                        drift.append(f"environment variable {fields['name']} differs")
                # A PATH entry must still be present, compared in normalised spelling.
                elif effect.kind == "path":
                    entries = [normalize_entry(item) for item in self.system.get_path(self.scope.mode)]
                    if normalize_entry(str(fields["entry"])) not in entries:
                        drift.append(f"PATH entry missing: {fields['entry']}")
                else:
                    unverified.append(effect.kind)
            except KeyError as exc:
                drift.append(f"malformed {effect.kind} effect: no {exc.args[0]}")
        return tuple(drift), tuple(unverified)

    def _resolve_path(self, parsed: Selector, distribution: bool = False) -> Resolved:
        """Resolve a path selector: a package root, a version directory, ``_check_updates`` or ``current``.

        The path must lie inside the root. A version directory outside it is accepted only with *distribution* (it is
        copied in); any other path outside is ``NotFound``.

        Parameters
        ----------
        parsed : Selector
            A selector of kind ``path``.
        distribution : bool, default=False
            Whether a version directory outside the root is allowed.

        Returns
        -------
        Resolved
            The view and the version the path names.

        Raises
        ------
        NotFound
            If the path does not exist, is not a package, or is outside the root.
        """
        # Normalise lexically so a trailing ``current`` is not followed before it is classified.
        candidate = Path(os.path.abspath(strip_extended_prefix(os.fspath(parsed.path.expanduser()))))
        version: str | None = None
        via_current = False
        is_tracker = False
        named = False

        # Classify the path: ``current``, a version or tracker directory, or a package root.
        if candidate.name.casefold() == "current":
            package_root = candidate.parent
            via_current = True
        elif candidate.is_dir() and (candidate / MANIFEST_NAME).is_file():
            package_root = candidate.parent
            if candidate.name == TRACKER_DIR_NAME:
                is_tracker = True
            elif is_version_name(candidate.name):
                version = candidate.name
                named = True  # the path names this version: install activates it, not the one that is current
            else:
                raise NotFound(f"{candidate} is not a version directory: its name is reserved or not a safe folder name")
        elif candidate.is_dir():
            package_root = candidate
        else:
            raise NotFound(f"Path does not exist: {candidate}")

        # Containment and trust checks come before the folder is scanned.
        inside = is_inside(package_root, self.scope.root)
        if not inside and not (distribution and version is not None):
            raise NotFound(f"{candidate} is not inside this scope's root ({self.scope.root})")
        elevated = self.scope.mode is Mode.SYSTEM
        self.scope.require_trusted(package_root, "the package folder")
        if not _is_local_package(package_root, allow_link=not elevated):
            raise NotFound(f"Not a package root, version directory or current: {candidate}")


        # Name the package relative to the root, then settle which version the path stands for.
        name = ntpath.relpath(real_path(package_root), real_path(self.scope.root)).replace("\\", "/") if inside else package_root.name
        view = self._view(name, package_root)
        if via_current:
            if view.local.current.status != "valid":
                raise NotFound(view.local.current.problem or f'"current" path does not exist: {candidate}')
            version = view.local.current.version

        # With no version in the path: the active one, else the newest valid local one.
        elif version is None and not is_tracker:
            if view.installed is not None:
                version, via_current = view.installed.version, True
            else:
                valid = view.local.valid_versions
                version = valid[-1].name if valid else None
        return Resolved(view, version, via_current, is_tracker, named_version=named)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _package_path(root: Path, name: str) -> Path | None:
    """Return the package root of *name* in *root*, or ``None`` for a name that could leave the root."""
    parts = name.split("/")
    if any(not part or part in (".", "..") or part.startswith(".") or ":" in part or "\\" in part for part in parts):
        return None
    return root.joinpath(*parts)


def _is_local_package(path: Path, *, allow_link: bool = False) -> bool:
    """Return whether *path* is a package root; a link is accepted only for an explicit path (*allow_link*)."""
    try:
        return path.is_dir() and (allow_link or not is_reparse_point(path)) and looks_like_package(path)
    except OSError:
        return False


# Parsed install records by file, valid while the file's identity, size and modification time are the same: a process that runs many
# commands (a library caller, the tests) does not parse every record again for each one. The file is still looked at, and never
# followed through a link, every time.
_RECORD_EFFECTS: dict[str, tuple[tuple[int, int, int], tuple[Effect, ...]]] = {}


def _record_effects(package_path: Path) -> tuple[Effect, ...]:
    """Return the effects of the install record of *package_path*; none when it has no usable record (``describe`` reports why)."""
    # Look at the file itself, never through a link; a missing record has no effects.
    file = record_path(package_path)
    try:
        folder_info, info = os.lstat(file.parent), os.lstat(file)
    except OSError:
        return ()
    if is_link_info(folder_info) or is_link_info(info):
        return ()

    # Reuse the cached parse while the file's identity, size and modification time are unchanged.
    stamp = (info.st_ino, info.st_size, info.st_mtime_ns)
    known = _RECORD_EFFECTS.get(str(file))
    if known is not None and known[0] == stamp:
        return known[1]
    record = read_install_record(package_path).record
    effects = record.effects if record is not None else ()
    _RECORD_EFFECTS[str(file)] = (stamp, effects)
    return effects


def manifest_modes(manifest_path: Path) -> tuple[str, ...] | None:
    """Return the ``modes`` of the ``pkg.toml`` at *manifest_path*, or ``None`` when it cannot be read.

    Parameters
    ----------
    manifest_path : Path
        The ``pkg.toml`` file.

    Returns
    -------
    tuple of str or None
        The modes the manifest allows, or ``None`` for an unreadable or invalid manifest.
    """
    try:
        return parse_manifest(manifest_path.read_text(encoding="utf-8-sig")).modes
    except (OSError, UnicodeDecodeError, ManifestError, ValueError):
        return None


def _matches(view: PackageView, filters: ListFilter) -> bool:
    """Return whether *view* passes *filters*."""
    if filters.statuses is not None and view.status not in filters.statuses:
        return False
    if filters.query is not None and filters.query.casefold() not in view.name.casefold():
        return False
    if filters.registry is not None and (view.registry is None or view.registry.registry.casefold() != filters.registry.casefold()):
        return False
    return True
