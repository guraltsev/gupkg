"""Describe what gupkg knows about one package as immutable data, and derive its status.

A ``PackageView`` joins the facts about a package: the local package root (its version
directories, tracker and ``current``), the install record, the registry entry it follows
and what is installed. ``view.status`` derives the one status shown to users
(``available``, ``local``, ``installed``, ``outdated`` or ``broken``; a version that fails
validation is ``invalid`` on its own), and ``known_candidates(view)`` lists the versions
an install or update may choose from. The module also parses and renders the install
record ``<package>\\.gupkg\\package.toml`` and the ``adhoc`` list of ``state.toml`` as plain
data. It does no input or output and never looks at a disk.

Usage and API
-------------
``gupkg.catalog`` builds the views; callers read ``PackageView.status``,
``PackageView.local``, ``PackageView.registry`` and friends. ``parse_install_record(data)``
and ``install_record_to_data(record)`` convert between the TOML data of an install record
and ``InstallRecord``. ``parse_state(data)`` reads the ad-hoc roots of ``state.toml``.

Implementation Approach
-----------------------
Every field is a frozen dataclass of plain values, so a view can be compared, cached and
shared between front ends. Facts are collected elsewhere (``gupkg.catalog``); the status
and the candidate list are pure functions of those facts, using the version ordering and
reserved names of ``gupkg.model.version``. A local version directory of the same name as
a registry version always wins over it, because a copy never overwrites (design 5.3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path
from typing import Any

from .manifest import Manifest
from .version import compare_versions

MODES = ("system", "user", "portable")

# The statuses of design 5.2 that describe a whole package; ``invalid`` belongs to one version.
STATUSES = ("available", "local", "installed", "outdated", "broken")

# The schema number written into every install record.
RECORD_SCHEMA = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RecordError(ValueError):
    """Report an install record or ``state.toml`` that is not valid, listing every problem."""

    def __init__(self, problems: str | list[str] | tuple[str, ...]) -> None:
        """Build the error from one message or a list of messages."""
        self.problems = (problems,) if isinstance(problems, str) else tuple(problems)
        super().__init__("; ".join(self.problems))


# ---------------------------------------------------------------------------
# Roots, links and the install record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Root:
    """A directory that holds packages, with one install mode.

    ``mode`` is ``None`` for a path that no configured root contains and no install record
    decides (``auto`` then applies, design 6.2). ``adhoc`` marks a root recorded in ``state.toml`` rather than configured in the
    settings; ``single`` says that its ``path`` is itself one package root.
    """

    path: Path
    mode: str | None
    adhoc: bool = False
    single: bool = False


@dataclass(frozen=True)
class AdhocRoot:
    """One ``[[adhoc]]`` entry of ``state.toml``: a package root outside every configured root."""

    path: str
    mode: str


@dataclass(frozen=True)
class Link:
    """Which registry entry a local package follows, as recorded in its install record.

    With neither ``registry`` nor ``name`` set (and ``unlinked`` false) the package follows
    the entry with its own name (decision D2). ``unlinked`` is the opt-out written by
    ``gupkg link --unlink``. ``revision`` and ``tracker_sha256`` remember which registry
    revision and which tracker content were last copied in.
    """

    registry: str | None = None
    name: str | None = None
    revision: str | None = None
    tracker_sha256: str | None = None
    unlinked: bool = False


@dataclass(frozen=True)
class Effect:
    """One integration effect applied by an install (written by the engine, issue 028)."""

    kind: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    adopted: bool = False


@dataclass(frozen=True)
class InstallRecord:
    """The content of ``<package>\\.gupkg\\package.toml``: mode, link and applied effects."""

    mode: str | None = None
    link: Link | None = None
    effects: tuple[Effect, ...] = ()


def _string(table: Mapping[str, Any], key: str, where: str, problems: list[str]) -> str | None:
    """Return a non-empty string value, or ``None`` when absent or wrong (recording the problem)."""
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}{key} must be a non-empty string")
        return None
    return value


def _check_keys(table: Mapping[str, Any], allowed: tuple[str, ...], where: str, problems: list[str]) -> None:
    """Record every key of *table* that is not in *allowed*."""
    for key in table:
        if key not in allowed:
            problems.append(f"unknown key '{where}{key}'; allowed keys: {', '.join(allowed)}")


def parse_install_record(data: Mapping[str, Any]) -> InstallRecord:
    """Validate the parsed TOML of an install record.

    Parameters
    ----------
    data : mapping
        The parsed ``package.toml`` of a package's ``.gupkg`` folder.

    Returns
    -------
    InstallRecord
        The typed record.

    Raises
    ------
    RecordError
        If the schema, a key or a value is not valid; the error lists every problem.
    """
    problems: list[str] = []
    _check_keys(data, ("schema", "mode", "link", "effect"), "", problems)
    if data.get("schema") != RECORD_SCHEMA:
        problems.append(f"unsupported install record schema ({data.get('schema', 'missing')!r}); expected {RECORD_SCHEMA}")

    # The install mode that the package was installed with.
    mode = _string(data, "mode", "", problems)
    if mode is not None and mode not in MODES:
        problems.append(f"mode must be one of {', '.join(MODES)}")
        mode = None

    # The link: a followed registry entry, or the opt-out, never both.
    link: Link | None = None
    if "link" in data:
        table = data["link"]
        if not isinstance(table, dict):
            problems.append("link must be a table")
        else:
            _check_keys(table, ("registry", "name", "revision", "tracker_sha256", "unlinked"), "link.", problems)
            unlinked = table.get("unlinked", False)
            if not isinstance(unlinked, bool):
                problems.append("link.unlinked must be true or false")
                unlinked = False
            link = Link(
                registry=_string(table, "registry", "link.", problems),
                name=_string(table, "name", "link.", problems),
                revision=_string(table, "revision", "link.", problems),
                tracker_sha256=_string(table, "tracker_sha256", "link.", problems),
                unlinked=unlinked,
            )
            if link.tracker_sha256 is not None and not _SHA256.fullmatch(link.tracker_sha256):
                problems.append("link.tracker_sha256 must be 64 lower-case hexadecimal digits")
            if link.unlinked and (link.registry or link.name):
                problems.append("link.unlinked cannot be combined with link.registry or link.name")

    # The applied effects: free-form tables with a kind, kept as written.
    effects: list[Effect] = []
    raw_effects = data.get("effect", [])
    if not isinstance(raw_effects, list) or any(not isinstance(item, dict) for item in raw_effects):
        problems.append("effect must be an array of tables ([[effect]])")
        raw_effects = []
    for index, item in enumerate(raw_effects):
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            problems.append(f"effect[{index}].kind must be a non-empty string")
            continue
        adopted = item.get("adopted", False)
        if not isinstance(adopted, bool):
            problems.append(f"effect[{index}].adopted must be true or false")
            adopted = False
        extra = {key: value for key, value in item.items() if key not in ("kind", "adopted")}
        effects.append(Effect(kind, extra, adopted))
    if problems:
        raise RecordError(problems)
    return InstallRecord(mode, link, tuple(effects))


def install_record_to_data(record: InstallRecord) -> dict[str, Any]:
    """Return the TOML-ready data of an install record.

    Parameters
    ----------
    record : InstallRecord
        The record to write.

    Returns
    -------
    dict
        Plain data for ``gupkg.tomlio``; unset values are left out.
    """
    data: dict[str, Any] = {"schema": RECORD_SCHEMA}
    if record.mode is not None:
        data["mode"] = record.mode
    if record.link is not None:
        link = {
            key: value
            for key, value in (
                ("registry", record.link.registry),
                ("name", record.link.name),
                ("revision", record.link.revision),
                ("tracker_sha256", record.link.tracker_sha256),
            )
            if value is not None
        }
        if record.link.unlinked:
            link["unlinked"] = True
        data["link"] = link
    if record.effects:
        data["effect"] = [
            {"kind": effect.kind, **({"adopted": True} if effect.adopted else {}), **dict(effect.fields)}
            for effect in record.effects
        ]
    return data


def parse_state(data: Mapping[str, Any]) -> tuple[AdhocRoot, ...]:
    """Read the ad-hoc roots from the parsed ``state.toml`` of a root data directory.

    Parameters
    ----------
    data : mapping
        The parsed ``state.toml``. Keys other than ``adhoc`` (the last registry sync, for
        example) belong to other readers and are ignored.

    Returns
    -------
    tuple of AdhocRoot
        One entry per ``[[adhoc]]`` table.

    Raises
    ------
    RecordError
        If ``adhoc`` is not an array of tables with an absolute ``path`` and a valid ``mode``.
    """
    raw = data.get("adhoc", [])
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise RecordError("adhoc must be an array of tables ([[adhoc]])")

    # Each entry needs an absolute path with a drive and one of the three modes.
    problems: list[str] = []
    entries: list[AdhocRoot] = []
    for index, item in enumerate(raw):
        path, mode = item.get("path"), item.get("mode")
        if not isinstance(path, str) or not re.match(r"^[A-Za-z]:[\\/]", path):
            problems.append(f"adhoc[{index}].path must be an absolute path with a drive")
        elif mode not in MODES:
            problems.append(f"adhoc[{index}].mode must be one of {', '.join(MODES)}")
        else:
            entries.append(AdhocRoot(path, mode))
    if problems:
        raise RecordError(problems)
    return tuple(entries)


# ---------------------------------------------------------------------------
# Facts about the local package root
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionState:
    """One version directory.

    ``pinned`` says that its ``pkg.toml`` is a valid format 2 version manifest (a concrete
    source). ``problems`` is empty exactly when the version is valid; a version with
    problems is ``invalid`` and takes no part in installs and updates.
    """

    name: str
    path: Path
    pinned: bool
    populated: bool
    problems: tuple[str, ...] = ()
    manifest: Manifest | None = None
    pkg_toml_sha256: str | None = None

    @property
    def valid(self) -> bool:
        """Return whether the version directory passes validation."""
        return not self.problems

    @property
    def status(self) -> str:
        """Return ``invalid``, ``available`` (valid, no ``App`` yet) or ``populated``."""
        if self.problems:
            return "invalid"
        return "populated" if self.populated else "available"


@dataclass(frozen=True)
class TrackerState:
    """The tracker ``_check_updates``; ``sha256`` is the content hash of the whole directory."""

    path: Path
    problems: tuple[str, ...] = ()
    manifest: Manifest | None = None
    sha256: str | None = None

    @property
    def valid(self) -> bool:
        """Return whether the tracker passes validation."""
        return not self.problems


@dataclass(frozen=True)
class CurrentState:
    """What the ``current`` entry of a package root is.

    ``status`` is ``absent`` (no ``current``), ``valid`` (a junction to a version directory
    of this package root), ``not-a-link``, ``dangling`` (the target is missing),
    ``escapes`` (the target lies outside the package root or is not one of its direct
    children) or ``bad-target`` (not a version directory with a ``pkg.toml``).
    """

    status: str
    version: str | None = None
    target: Path | None = None
    problem: str | None = None


@dataclass(frozen=True)
class LocalState:
    """A package root on disk: version directories (oldest first), tracker and ``current``."""

    path: Path
    versions: tuple[VersionState, ...] = ()
    tracker: TrackerState | None = None
    current: CurrentState = CurrentState("absent")

    def version(self, name: str) -> VersionState | None:
        """Return the version directory called *name* (letter case ignored), or ``None``."""
        folded = name.casefold()
        return next((item for item in self.versions if item.name.casefold() == folded), None)

    @property
    def valid_versions(self) -> tuple[VersionState, ...]:
        """Return the valid version directories, oldest first."""
        return tuple(item for item in self.versions if item.valid)


@dataclass(frozen=True)
class InstalledState:
    """The active version of a package whose ``current`` is valid.

    ``drift`` lists install-record effects that the system no longer has, and
    ``unverified`` the effect kinds that could not be verified (both are only filled in
    when the catalog is asked to verify effects).
    """

    version: str
    path: Path
    app_populated: bool
    app_problem: str | None = None
    effects: tuple[Effect, ...] = ()
    drift: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Facts about the registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryVersion:
    """One pinned version of a registry entry, with its fingerprint when the registry publishes one."""

    name: str
    path: Path
    fingerprint: str | None = None
    pkg_toml_sha256: str | None = None


@dataclass(frozen=True)
class RegistryTracker:
    """The tracker of a registry entry; ``sha256`` is the content hash of its directory."""

    path: Path
    sha256: str


@dataclass(frozen=True)
class RegistryEntry:
    """One package of a registry: its valid pinned versions (oldest first), tracker and revision."""

    registry: str
    name: str
    revision: str | None
    path: Path
    versions: tuple[RegistryVersion, ...] = ()
    tracker: RegistryTracker | None = None

    def version(self, name: str) -> RegistryVersion | None:
        """Return the pinned version called *name* (letter case ignored), or ``None``."""
        folded = name.casefold()
        return next((item for item in self.versions if item.name.casefold() == folded), None)


@dataclass(frozen=True)
class Candidate:
    """A version that an install or update may choose, with where it comes from.

    ``source`` is ``local`` (a version directory in the package root) or ``registry``
    (a pinned version that would be copied in from ``registry``).
    """

    version: str
    source: str
    path: Path
    registry: str | None = None
    fingerprint: str | None = None


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageView:
    """Everything known about one package, built by ``gupkg.catalog.Catalog.describe``.

    Attributes
    ----------
    name : str
        The package name (folder names joined by ``/`` for a package in grouping folders).
    root : Root or None
        The root that holds the package; ``None`` for a registry entry with no local package.
    local : LocalState or None
        The package root on disk, or ``None`` when there is none.
    registry : RegistryEntry or None
        The registry entry the package follows (the same-name entry by default, D2).
    unfollowed : RegistryEntry or None
        The same-name entry of a registry the package opted out of, for display only.
    link : Link or None
        The link of the install record.
    installed : InstalledState or None
        The active version, when ``current`` is valid.
    diverged : tuple of str
        Local versions whose ``pkg.toml`` differs from the followed registry's copy.
    problems : tuple of str
        Problems that make the package ``broken``: an unreadable install record, or a
        recorded mode that disagrees with the root.
    warnings : tuple of str
        Problems that do not (a link to a registry entry that is missing, for example).
    record : InstallRecord or None
        The parsed install record.
    mode : str or None
        The install mode: the record's, else the root's.
    path : Path or None
        The package root directory.
    status : str
        The status of design 5.2.
    """

    name: str
    root: Root | None
    local: LocalState | None
    registry: RegistryEntry | None = None
    unfollowed: RegistryEntry | None = None
    link: Link | None = None
    installed: InstalledState | None = None
    diverged: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    record: InstallRecord | None = None

    @property
    def mode(self) -> str | None:
        """Return the install mode: the install record's, else the root's."""
        if self.record is not None and self.record.mode is not None:
            return self.record.mode
        return self.root.mode if self.root is not None else None

    @property
    def path(self) -> Path | None:
        """Return the package root directory, or ``None`` when there is no local package."""
        return self.local.path if self.local is not None else None

    @property
    def following(self) -> str | None:
        """Return ``registry/name`` of the entry the package follows, or ``None``."""
        return f"{self.registry.registry}/{self.registry.name}" if self.registry is not None else None

    @property
    def note(self) -> str | None:
        """Return the text shown beside a package that opted out of its same-name registry entry."""
        if self.unfollowed is None:
            return None
        return f"same name in registry: {self.unfollowed.registry} (not followed)"

    @property
    def broken_reasons(self) -> tuple[str, ...]:
        """Return why the package is ``broken``: ``current``, the active ``App``, drift or the record."""
        reasons: list[str] = []
        if self.local is not None:
            current = self.local.current
            if current.status not in ("absent", "valid"):
                reasons.append(current.problem or f"current is {current.status}")
        if self.installed is not None:
            if not self.installed.app_populated:
                reasons.append(self.installed.app_problem or f"the App of {self.installed.version} is missing or empty")
            reasons.extend(f"drift: {item}" for item in self.installed.drift)
        reasons.extend(self.problems)
        return tuple(reasons)

    @property
    def status(self) -> str:
        """Return the status of design 5.2: ``available``, ``local``, ``installed``, ``outdated`` or ``broken``.

        A valid ``current`` whose populated active version fails format 2 validation is still
        ``installed``: ``invalid`` belongs to that version alone (design 5.2) and never blocks the package.
        """
        if self.local is None:
            return "available"
        if self.broken_reasons:
            return "broken"
        if self.installed is None:
            return "local"
        return "outdated" if known_candidates(self) else "installed"


def known_candidates(view: PackageView) -> tuple[Candidate, ...]:
    """Return the versions an install or update may choose from, newest first.

    Parameters
    ----------
    view : PackageView
        The package. Nothing is read: a tracker is never run (D11).

    Returns
    -------
    tuple of Candidate
        The valid local version directories (including those written by earlier
        ``check-updates`` runs) and the pinned versions of the followed registry entry.
        A registry version is left out when a local directory of the same name exists,
        because a copy never overwrites. For an installed package only versions newer than
        ``current`` are listed. Every pinned version the registry holds is offered (D36).
    """
    local_names = {item.name.casefold() for item in view.local.versions} if view.local is not None else set()
    found: list[Candidate] = []
    if view.local is not None:
        found.extend(Candidate(item.name, "local", item.path) for item in view.local.valid_versions)
    if view.registry is not None:
        found.extend(
            Candidate(item.name, "registry", item.path, view.registry.registry, item.fingerprint)
            for item in view.registry.versions
            if item.name.casefold() not in local_names
        )

    # An installed package only moves forward; ``switch`` is the explicit way back (design 5.3 rule 4).
    if view.installed is not None:
        found = [item for item in found if compare_versions(item.version, view.installed.version) > 0]

    # Newest first; sorting is stable, so a local directory stays ahead of a registry version of the same name.
    found.sort(key=cmp_to_key(lambda left, right: compare_versions(right.version, left.version)))
    return tuple(found)
