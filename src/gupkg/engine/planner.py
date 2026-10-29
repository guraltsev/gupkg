"""Plan one operation on one package as data, without touching the machine (design 13.1 and 13.3).

``plan_install`` serves ``install``, ``update``, ``switch`` and repairs: from the package view, the chosen
version and the effects the version declares it works out what to copy in, whether ``App`` is populated and
from where, whether ``current`` moves, and which integration effects are added, updated, kept and removed
against the install record. ``plan_uninstall``, ``plan_cleanup`` and ``plan_check_updates`` do the same for
the other operations. A plan never contains a tracker step except ``check-updates``' own (decision D11).

Usage and API
-------------
Gather the facts (the view, the chosen version with its manifest, the desired effects) and call the matching
``plan_*`` function. The result is an ``InstallPlan`` of ``gupkg.model.plan``; ``plan.to_data()`` is the
``--plan --json`` document. ``diff_effects(recorded, desired)`` is the one place that decides which effects
are added, updated, kept or removed. ``PlanError`` (exit code 2) reports a request that cannot be planned,
such as an ``App`` that is missing with no way to rebuild it.

Implementation Approach
-----------------------
Planning is a pure function of its arguments: it reads no file and calls no ``System`` method, so each rule is
tested on plain data. The shipped-``App`` rules live here: a shipped ``App`` is never dropped by cleanup, never
replaced by ``--refresh``, and recovered from its ``download_url`` only when it is missing. Facts the plan needs
that only a disk can answer (whether a script's recorded inputs still exist, which receipts there are) are
passed in as ``CleanupFacts``.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from ..model.checksum import ChecksumPlan, plan_checksum
from ..model.manifest import APP_MISSING_ERROR, INSTALLER_LABEL, Manifest, Source, invalid_checksum_problem
from ..model.plan import (
    CHECK_UPDATES_PHASES,
    INSTALL_PHASES,
    CopyIn,
    InstallPlan,
    Phase,
    PlanStep,
    PlannedEffect,
    PopulatePlan,
    Request,
    effect_key,
    normalize_entry,
)
from ..model.scope import Scope, check_supported
from ..model.state import Candidate, Effect, PackageView, VersionState
from ..model.version import compare_versions

EXIT_PLAN = 2


class PlanError(ValueError):
    """Report a request that cannot be planned; the message says what to do instead."""

    exit_code = EXIT_PLAN


class NameConflict(PlanError):
    """Refuse a package that would provide a name another installed package already provides; the stable code is ``name-conflict``."""

    error_code = "name-conflict"

    def __init__(self, message: str, *, name: str, packages: "Sequence[str]") -> None:
        """Build the message; *name* and *packages* (the packages that claim it) are the details of the failure document."""
        super().__init__(message)
        self.error_details = {"name": name, "packages": list(packages)}


# What a package puts on the machine under a name of its own: a shim or wrapper in ``bin`` (identified by its path), a Start Menu
# shortcut (its path) and an environment variable (scope and name). A ``PATH`` entry is shared by design and is not a name.
_NAME_WORDS = {"shim": "shim", "file": "wrapper", "shortcut": "shortcut", "env": "environment variable"}


@dataclass(frozen=True)
class ProvidedName:
    """One name that an effect provides: its comparison ``key``, its words for a message and where it lives."""

    key: tuple[str, str]
    word: str
    name: str
    where: str
    primary: tuple[str, str] | None = None  # for a shim's configuration file: the key of the shim itself


def provided_names(effects: Sequence[Effect]) -> dict[tuple[str, str], ProvidedName]:
    """Return the names *effects* provide, by comparison key (letter case ignored).

    A shim and a wrapper file share one namespace, the path in ``bin``; a shortcut is its ``.lnk`` path; an environment
    variable is its scope and name. ``path`` effects and everything else provide no name.
    """
    found: dict[tuple[str, str], ProvidedName] = {}
    for effect in effects:
        word = _NAME_WORDS.get(effect.kind)
        if word is None:
            continue
        fields = effect.fields
        if effect.kind == "env":
            scope, name = str(fields.get("scope", "")), str(fields.get("name", ""))
            key = ("env", f"{scope}:{name}".casefold())
            found.setdefault(key, ProvidedName(key, word, name, f"{scope} environment"))
            continue
        path = str(fields.get("path", ""))
        group = "shortcut" if effect.kind == "shortcut" else "file"
        key = (group, os.path.normcase(os.path.normpath(path)).casefold())
        found.setdefault(key, ProvidedName(key, word, path.rsplit("\\", 1)[-1], path))
        shim_key = key

        # A launcher shim also writes ``<name>.config.toml`` into ``bin``: the same namespace as a wrapper file.
        config = str(fields.get("config", "")) if effect.kind == "shim" else ""
        if config:
            key = ("file", os.path.normcase(os.path.normpath(config)).casefold())
            found.setdefault(key, ProvidedName(key, "shim configuration file", config.rsplit("\\", 1)[-1], config, shim_key))
    return found


class NameIndex:
    """Which installed packages provide each name, built once from the install records and updated one package at a time.

    ``owners(exclude)`` is the read-only view the checks use: the first package (by name, letter case ignored) that provides a
    name and is not *exclude*. Replacing or dropping one package costs only that package's names, so a batch never rescans.
    """

    def __init__(self, installed: Mapping[str, Sequence[Effect]] | None = None) -> None:
        """Index *installed*, a map from package name to the effects of its install record."""
        self._by_key: dict[tuple[str, str], list[str]] = {}
        self._keys: dict[str, tuple[tuple[str, str], ...]] = {}
        for package, effects in (installed or {}).items():
            self.set(package, effects)

    def set(self, package: str, effects: Sequence[Effect]) -> None:
        """Record that *package* provides the names of *effects* (replacing what it provided before)."""
        self.drop(package)
        keys = tuple(provided_names(effects))
        self._keys[package] = keys
        for key in keys:
            owners = self._by_key.setdefault(key, [])
            owners.append(package)
            owners.sort(key=str.casefold)

    def drop(self, package: str) -> None:
        """Forget the names of *package* (it was uninstalled or has no record)."""
        for key in self._keys.pop(package, ()):
            owners = self._by_key[key]
            owners.remove(package)
            if not owners:
                del self._by_key[key]

    def owners(self, exclude: str) -> "_Owners":
        """Return the owners of every name for a package named *exclude*, which never owns a name against itself."""
        return _Owners(self._by_key, exclude.casefold())


class _Owners(Mapping):
    """Read-only map from a name key to the package that owns it, leaving out one package."""

    def __init__(self, by_key: Mapping[tuple[str, str], list[str]], exclude: str) -> None:
        self._by_key, self._exclude = by_key, exclude

    def __getitem__(self, key: tuple[str, str]) -> str:
        for package in self._by_key.get(key, ()):
            if package.casefold() != self._exclude:
                return package
        raise KeyError(key)

    def __iter__(self):
        return (key for key in self._by_key if key in self)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __bool__(self) -> bool:
        """Return whether any package provides a name (cheap; a name of *exclude* alone still counts and is skipped on lookup)."""
        return bool(self._by_key)


def name_owners(installed: Mapping[str, Sequence[Effect]], *, exclude: str) -> dict[tuple[str, str], str]:
    """Return which installed package provides each name: the recorded effects of every package but *exclude* (letter case ignored).

    *installed* maps a package name to the effects of its install record; the first package (by name) that claims a name owns it.
    """
    return dict(NameIndex(installed).owners(exclude))


def _refusal(package: str, other: str, item: ProvidedName) -> NameConflict:
    """Return the refusal of *package* for the name *item* that the installed package *other* already provides."""
    return NameConflict(
        f"{package} cannot be installed: it would provide the {item.word} '{item.name}' ({item.where}), which the installed "
        f"package '{other}' already provides. Uninstall '{other}' first, or use a package definition with a different name.",
        name=item.name, packages=(package, other),
    )


def check_names(package: str, desired: Sequence[Effect], owners: Mapping[tuple[str, str], str]) -> None:
    """Refuse *desired* when it provides a name that an installed package listed in *owners* provides.

    Raises
    ------
    NameConflict
        Naming both packages, the name and what to do; nothing has been changed.
    """
    for key, item in provided_names(desired).items():
        other = owners.get(key)
        if other is not None and other.casefold() != package.casefold():
            raise _refusal(package, other, item)


def check_names_between(wanted: Mapping[str, Sequence[Effect]]) -> dict[str, NameConflict]:
    """Find the packages of one command that provide the same name; return the refusal of each package involved.

    *wanted* maps a package name to the effects its operation would apply. The refusal names both packages of the first
    clash found for each.
    """
    claimed: dict[tuple[str, str], str] = {}
    refused: dict[str, NameConflict] = {}
    for package, effects in wanted.items():
        for key, item in provided_names(effects).items():
            other = claimed.setdefault(key, package)
            if other.casefold() == package.casefold():
                continue
            error = NameConflict(
                f"{other} and {package} both provide the {item.word} '{item.name}' ({item.where}). Install one of them, or "
                "give one of them a package definition with a different name.",
                name=item.name, packages=(other, package),
            )
            refused.setdefault(other, error)
            refused.setdefault(package, error)
    return refused


class InvalidVersion(PlanError):
    """Report that a version directory is not valid; a malformed declared hash has the stable code ``invalid-checksum``."""

    def __init__(self, message: str, *, version: str, problems: "Sequence[str]") -> None:
        """Build the message; *version* and *problems* decide the error code."""
        super().__init__(message)
        if invalid_checksum_problem(problems):
            self.error_code = "invalid-checksum"
            self.error_details = {"version": version, "field": "source.sha256"}


@dataclass(frozen=True)
class Chosen:
    """The version an operation works on.

    Attributes
    ----------
    candidate : Candidate
        The version and where it comes from (a local directory, a registry pinned version).
    manifest : Manifest
        Its parsed ``pkg.toml``.
    state : VersionState, optional
        The local version directory (the source folder of a distribution), when the version is on disk.
    """

    candidate: Candidate
    manifest: Manifest
    state: VersionState | None = None


@dataclass(frozen=True)
class CleanupFacts:
    """What only a disk can say about a package, for ``plan_cleanup``.

    ``receipts`` are the versions that have a receipt, ``work`` the entries of ``.gupkg\\work``, and
    ``inputs_present`` says per version whether the recorded inputs of a script-populated ``App`` still exist, and
    ``bundled`` names the versions whose directory holds a bundled ``gupkg`` folder. ``work`` lists only the entries old
    enough to remove.
    """

    receipts: tuple[str, ...] = ()
    work: tuple[str, ...] = ()
    inputs_present: Mapping[str, bool] = field(default_factory=dict)
    bundled: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupPlan:
    """What ``cleanup`` removes from one package: ``App`` folders, receipts and work entries, and what it keeps."""

    package: str
    drop_apps: tuple[str, ...] = ()
    kept_apps: tuple[tuple[str, str], ...] = ()
    drop_receipts: tuple[str, ...] = ()
    drop_work: tuple[str, ...] = ()
    drop_bundled: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """Return whether there is nothing to remove."""
        return not (self.drop_apps or self.drop_receipts or self.drop_work or self.drop_bundled)

    def to_data(self) -> dict[str, object]:
        """Return the JSON-ready description of the cleanup."""
        return {
            "package": self.package, "drop_apps": list(self.drop_apps),
            "kept_apps": [{"version": version, "why": why} for version, why in self.kept_apps],
            "drop_receipts": list(self.drop_receipts), "drop_work": list(self.drop_work),
            "drop_bundled": list(self.drop_bundled),
        }


# ---------------------------------------------------------------------------
# Install, update, switch and repair
# ---------------------------------------------------------------------------


def plan_install(
    request: Request,
    view: PackageView,
    context: Scope,
    chosen: Chosen,
    desired: Sequence[Effect],
    *,
    notes: Sequence[str] = (),
    copy_in: CopyIn | None = None,
    package_path: Path | None = None,
    url_from: str = "pkg.toml",
    drifted: Collection[tuple[str, ...]] = frozenset(),
    allow_installer: bool = True,
    owners: Mapping[tuple[str, str], str] | None = None,
) -> InstallPlan:
    """Plan an install-like operation: ``install``, ``update``, ``switch`` or a repair.

    Parameters
    ----------
    request : Request
        The request (``refresh`` rebuilds a recoverable ``App``).
    view : PackageView
        The package as it is now (an ``available`` view for a registry-only package).
    context : Scope
        The scope of the run.
    chosen : Chosen
        The version to activate and its manifest.
    desired : sequence of Effect
        The integration effects the version declares for this mode.
    notes : sequence of str
        Facts to report (the origin label, what the mode left out).
    copy_in : CopyIn, optional
        A version directory that is copied into the package root before anything else.
    package_path : Path, optional
        The package root when the view has none yet.
    url_from : str
        Where the download URL comes from, reported in the checksum plan.
    drifted : collection of tuple
        The ``effect_key`` of recorded effects that a repair found missing or changed on the machine.
    allow_installer : bool
        The ``hooks.allow_vendor_installers`` setting: whether a version that runs a vendor installer may be populated.
    owners : mapping, optional
        Which other installed package provides each name (``name_owners``); a name the version would provide that another
        package already provides is refused. A download-only update integrates nothing and is not checked.

    Returns
    -------
    InstallPlan
        The plan, with the phases of design 13.1.

    Raises
    ------
    PlanError
        If ``App`` is missing and cannot be rebuilt (``App is missing and no way to recover it.``), or the
        manifest does not support the mode.
    ModeRefused
        If the manifest's ``modes`` lack the scope's mode (the refusal of ``check_supported``).
    NameConflict
        If the version would provide a shim, wrapper, shortcut or environment variable that another installed package provides.
    """
    manifest = chosen.manifest
    version = chosen.candidate.version
    check_supported(context, manifest.modes, command=request.operation, name=view.name)
    if owners and not request.download_only:
        check_names(view.name, desired, owners)
    path = package_path or view.path or context.package_dir(view.name)
    previous = view.installed.version if view.installed is not None else None
    record_effects = view.record.effects if view.record is not None else ()

    # How App is obtained and whether anything has to happen.
    populate = plan_populate(
        manifest, chosen.state, refresh=request.refresh, url_from=url_from, copy_in=copy_in is not None,
        allow_installer=allow_installer,
    )

    # The integration diff against what the install record says is applied now. An effect that is recorded but
    # known to be missing on the machine (a repair found it) is applied again.
    effects = diff_effects(record_effects, desired)
    if drifted:
        effects = tuple(
            PlannedEffect("update", item.effect, item.effect) if item.action == "keep" and effect_key(item.effect) in drifted else item
            for item in effects
        )
    activating = previous is None or previous.casefold() != version.casefold() or (
        view.local is not None and view.local.current.status != "valid"
    )

    # A download-only update prepares the version and stops: nothing is activated or integrated.
    if request.download_only:
        effects, activating = (), False

    phases = _install_phases(request, view, context, chosen, populate, copy_in, effects, activating, previous, notes)
    return InstallPlan(
        request.operation, view.name, Path(path), context.root, context.mode, version, previous, populate, copy_in,
        effects, phases, tuple(notes), False,
        {"download_only": True} if request.download_only else {},
    )


def plan_populate(
    manifest: Manifest, state: VersionState | None, *, refresh: bool, url_from: str, copy_in: bool = False,
    allow_installer: bool = True,
) -> PopulatePlan:
    """Decide how the version's ``App`` is obtained and whether that has to happen now.

    A populated ``App`` stays unless ``refresh`` asks to rebuild one that can be rebuilt; a shipped ``App`` is
    never replaced. A missing ``App`` is rebuilt from the ``download_url`` or the populate script, whatever
    ``shipped`` says; with neither there is no way to recover it.

    Raises
    ------
    PlanError
        With ``App is missing and no way to recover it.`` when nothing can populate a missing ``App``.
    """
    source = manifest.source
    kind = "script" if source.populate == "script" else "shipped" if source.populate == "none" else (
        "git" if source.kind == "git" else "download"
    )
    populated = bool(state is not None and state.populated)
    recoverable = source.recoverable
    checksum = None
    if source.download_url is not None:
        git = source.git
        checksum = plan_checksum(
            source.download_url, source.sha256, url_from=url_from, commit=git.commit if git else None, git=kind == "git"
        )

    # A populated App is kept; only --refresh rebuilds it, and never a shipped one.
    if populated:
        if refresh and recoverable and not source.shipped:
            return _populate_plan(kind, True, "--refresh rebuilds the App", source, checksum, recoverable, allow_installer)
        why = "the shipped App is never replaced" if refresh and source.shipped else "App is already populated"
        return _populate_plan(kind, False, why, source, checksum, recoverable, allow_installer)
    if not recoverable:
        raise PlanError(APP_MISSING_ERROR)
    return _populate_plan(kind, True, "App is missing", source, checksum, recoverable, allow_installer)


def _populate_plan(
    kind: str, needed: bool, why: str, source: Source, checksum: ChecksumPlan | None, recoverable: bool, allow_installer: bool
) -> PopulatePlan:
    """Build a ``PopulatePlan``; a vendor installer that has to run is refused when the setting forbids it."""
    installer = source.installer.run if source.installer is not None and kind == "download" else None
    if installer is not None and needed and not allow_installer:
        raise PlanError(
            f"This version {INSTALLER_LABEL} ([source.installer]), and the setting hooks.allow_vendor_installers is false; "
            "nothing was installed."
        )
    if installer is not None and needed and (checksum is None or checksum.sha256 != "present"):
        raise PlanError(
            f"This version {INSTALLER_LABEL} ([source.installer]) and pins no sha256. A vendor installer runs only from a "
            "download that matches a pinned sha256 (--skip-checksum does not apply to it); pin one with `gupkg pkg pin`. "
            "Nothing was installed."
        )
    return PopulatePlan(kind, needed, why, source.download_url, checksum, source.script, recoverable, installer)


def diff_effects(recorded: Sequence[Effect], desired: Sequence[Effect]) -> tuple[PlannedEffect, ...]:
    """Diff the desired effects against the install record.

    Parameters
    ----------
    recorded : sequence of Effect
        The effects the install record says are applied.
    desired : sequence of Effect
        The effects the chosen version declares.

    Returns
    -------
    tuple of PlannedEffect
        ``add`` for a new effect, ``update`` for one with the same identity and other fields, ``keep`` for an
        identical one (the recorded effect is kept, so facts such as ``added`` survive) and ``remove`` for a
        recorded effect that is no longer wanted. Removals come last.
    """
    before = {effect_key(item): item for item in recorded}
    wanted: dict[tuple[str, ...], Effect] = {}
    result: list[PlannedEffect] = []
    for item in desired:
        key = effect_key(item)
        wanted[key] = item
        old = before.get(key)
        if old is None:
            result.append(PlannedEffect("add", item))
        elif _comparable(old) == _comparable(item):
            result.append(PlannedEffect("keep", old))
        else:
            result.append(PlannedEffect("update", item, old))
    for key, old in before.items():
        if key not in wanted:
            result.append(PlannedEffect("remove", old, old))
    return tuple(result)


def _comparable(effect: Effect) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Return the part of an effect that decides whether it changed (the run-time fact ``added`` does not).

    A ``PATH`` entry is compared the way ``PATH`` entries are (case, trailing separator), not as written.
    """
    fields = {key: value for key, value in effect.fields.items() if key != "added"}
    if effect.kind == "path" and "entry" in fields:
        fields["entry"] = normalize_entry(str(fields["entry"]))
    return effect.kind, tuple(sorted((key, repr(value)) for key, value in fields.items()))


def _effect_line(item: PlannedEffect) -> PlanStep:
    """Return the Integrate line of one planned effect."""
    fields = item.effect.fields
    label = str(fields.get("name") or fields.get("entry") or fields.get("path") or "")
    label = label.rsplit("\\", 1)[-1] if item.effect.kind in ("shortcut", "shim", "file") else label
    sign = {"add": "+", "update": "~", "remove": "-", "keep": "="}[item.action]
    suffix = "  stale, removed" if item.action == "remove" else ""
    return PlanStep(f"{sign} {item.effect.kind}  {label}{suffix}", "remove" if item.action == "remove" else "add")


def _install_phases(
    request: Request,
    view: PackageView,
    context: Scope,
    chosen: Chosen,
    populate: PopulatePlan,
    copy_in: CopyIn | None,
    effects: Sequence[PlannedEffect],
    activating: bool,
    previous: str | None,
    notes: Sequence[str],
) -> tuple[Phase, ...]:
    """Build the phases of an install-like plan, each with its readable steps."""
    version = chosen.candidate.version
    where = {"local": "local version directory", "registry": f"registry {chosen.candidate.registry}"}.get(
        chosen.candidate.source, chosen.candidate.source
    )
    move = f"{view.name}  {previous}  ->  {version}" if previous and previous != version else f"{view.name}  {version}"
    resolve = [PlanStep(f"{move} ({where})"), PlanStep(context.describe())]
    if copy_in is not None:
        origin = "a distribution folder" if copy_in.origin == "distribution" else f"registry {copy_in.registry}"
        resolve.append(PlanStep(f"copy {copy_in.source} into the package root (from {origin})"))
    resolve.extend(PlanStep(text) for text in notes)

    fetching = populate.needed and populate.kind == "download"
    verifying = populate.needed and populate.kind in ("download", "git")
    building = populate.needed
    skip = "App is already populated" if not populate.needed else ""
    steps: dict[str, list[PlanStep]] = {name: [] for name in INSTALL_PHASES}
    steps["Resolve"] = resolve
    if fetching:
        steps["Fetch"].append(PlanStep(f"download {populate.url}"))
    if verifying:
        steps["Verify"].append(PlanStep(f"checksum: {populate.checksum.sha256 if populate.checksum else 'n/a'}"))
    if building:
        verb = {"script": f"run the populate script {populate.script}", "download": "unpack the download",
                "git": "check out the repository", "shipped": "recover the App"}[populate.kind]
        if populate.installer is not None:
            verb = f"{INSTALLER_LABEL}: {' '.join(populate.installer)}"
        steps["Unpack"].append(PlanStep(verb))
        steps["Prepare"].append(PlanStep("run the prepare steps, rename the finished App into place"))
    if activating:
        if chosen.manifest.portable and chosen.manifest.portable.persist:
            steps["Activate"].append(PlanStep(f"hand over persisted data: {', '.join(chosen.manifest.portable.persist)}"))
        steps["Activate"].append(PlanStep(f"switch current to {version}"))
    steps["Integrate"] = [_effect_line(item) for item in effects if item.action != "keep"]
    steps["Finish"] = [PlanStep("write the receipt and the install record")]

    skipped_why = {
        "Fetch": (skip or "nothing is downloaded") if not fetching else "",
        "Verify": (skip or "nothing is downloaded") if not verifying else "",
        "Unpack": skip if not building else "",
        "Prepare": skip if not building else "",
        "Activate": ("download only: nothing is activated" if request.download_only else "current already points at this version")
        if not activating else "",
        "Integrate": ("download only: nothing is integrated" if request.download_only else "no effect changes")
        if not steps["Integrate"] else "",
    }
    return tuple(
        Phase(name, tuple(steps[name]), bool(skipped_why.get(name)), skipped_why.get(name, "")) for name in INSTALL_PHASES
    )


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def plan_uninstall(request: Request, view: PackageView, context: Scope) -> InstallPlan:
    """Plan ``uninstall``: remove every recorded effect, then ``current``; ``--purge`` also deletes the package root.

    The version directories (and their ``App`` folders, shipped ones above all) stay, so the package becomes
    ``local`` again. ``extra`` names what ``--purge`` would delete and which ``App`` folders cannot be rebuilt,
    which is what the confirmation shows.

    Raises
    ------
    PlanError
        If there is no package on disk.
    """
    if view.local is None or view.path is None:
        raise PlanError(f"'{view.name}' is not installed")
    effects = tuple(PlannedEffect("remove", item, item) for item in (view.record.effects if view.record else ()))
    previous = view.installed.version if view.installed is not None else None

    # What --purge would destroy: the data folder and every App that cannot be rebuilt.
    unrecoverable: list[str] = []
    for state in view.local.versions:
        if state.populated and state.manifest is not None and not state.manifest.source.recoverable:
            unrecoverable.append(state.name)
    extra: dict[str, object] = {}
    if request.purge:
        extra = {"purge": {"package_root": str(view.path), "data": str(view.path / "data"), "unrecoverable_apps": unrecoverable}}
    steps = [
        PlanStep(f"{view.name}: uninstall"),
        PlanStep(context.describe()),
        *([PlanStep("--purge: delete the whole package root including data\\", "remove")] if request.purge else []),
    ]
    phases = (
        Phase("Resolve", tuple(steps)),
        Phase("Integrate", tuple(_effect_line(item) for item in effects), not effects, "no effect is recorded" if not effects else ""),
        Phase("Activate", (PlanStep("remove current", "remove"),), previous is None, "not installed" if previous is None else ""),
        Phase("Finish", (PlanStep("delete the package root" if request.purge else "write the install record"),)),
    )
    return InstallPlan(
        "uninstall", view.name, view.path, context.root, context.mode, previous, previous, None, None, effects, phases,
        (), request.purge, extra,
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def plan_cleanup(request: Request, view: PackageView, facts: CleanupFacts) -> CleanupPlan:
    """Plan ``cleanup`` for one package (design 13.3).

    Drops the ``App`` of recoverable versions that are not ``current`` beyond the newest ``keep`` older ones (when
    nothing is installed, beyond the newest ``keep`` versions); keeps every
    shipped ``App``, every ``App`` of a version newer than ``current`` (an update that ``check-updates`` prepared), and the
    ``App`` of a script-populated version whose inputs are gone (it could not be rebuilt). Also drops receipts of versions that no longer
    exist and everything in ``.gupkg\\work``. ``--bundled`` drops bundled gupkg copies of version directories.

    Raises
    ------
    PlanError
        If there is no package on disk.
    """
    if view.local is None:
        raise PlanError(f"'{view.name}' has no package root to clean")
    current = view.installed.version if view.installed is not None else None
    kept: list[tuple[str, str]] = []
    candidates: list[VersionState] = []
    for state in view.local.versions:
        if not state.populated:
            continue
        source = state.manifest.source if state.manifest is not None else None
        if current is not None and state.name.casefold() == current.casefold():
            kept.append((state.name, "current"))
        elif source is None:
            kept.append((state.name, "invalid version: kept"))
        elif source.shipped:
            kept.append((state.name, "shipped App is never dropped"))
        elif not source.recoverable:
            kept.append((state.name, "cannot be rebuilt"))
        elif source.populate == "script" and not facts.inputs_present.get(state.name, False):
            kept.append((state.name, "the populate script's inputs are gone"))
        elif current is not None and compare_versions(state.name, current) > 0:
            kept.append((state.name, "newer than current (an update is ready)"))
        else:
            candidates.append(state)

    # Keep the newest `keep` older versions; drop the rest.
    candidates.sort(key=cmp_to_key(lambda left, right: compare_versions(right.name, left.name)))
    for state in candidates[: request.keep]:
        kept.append((state.name, f"one of the {request.keep} newest older versions"))
    drop = tuple(state.name for state in candidates[request.keep:])
    existing = {state.name.casefold() for state in view.local.versions}
    receipts = tuple(item for item in facts.receipts if item.casefold() not in existing)
    present = {name.casefold() for name in facts.bundled}
    bundled = tuple(state.name for state in view.local.versions if state.name.casefold() in present) if request.bundled else ()
    return CleanupPlan(view.name, drop, tuple(kept), receipts, facts.work, bundled)


# ---------------------------------------------------------------------------
# Check for updates
# ---------------------------------------------------------------------------


def plan_check_updates(
    request: Request, view: PackageView, context: Scope, *, tracker_action: str, download: bool
) -> InstallPlan:
    """Plan ``check-updates`` (design 13.1, second table): run the tracker, write the version, populate it, stop.

    Parameters
    ----------
    request : Request
        The request.
    view : PackageView
        The package (it must be local).
    context : Scope
        The scope of the run.
    tracker_action : str
        What happens to the tracker first: ``none``, ``copy``, ``refresh`` or ``keep`` (``Catalog.tracker_sync``).
    download : bool
        Whether the new version is populated (no ``--no-download``).

    Returns
    -------
    InstallPlan
        The plan, with the phases of the check-updates table. It never activates and never integrates.
    """
    steps: dict[str, list[PlanStep]] = {name: [] for name in CHECK_UPDATES_PHASES}
    steps["Resolve"] = [PlanStep(f"{view.name}: check for updates"), PlanStep(context.describe())]
    if tracker_action in ("copy", "refresh"):
        steps["Resolve"].append(PlanStep(f"{tracker_action} the registry's tracker into the package root"))
    steps["Track"] = [PlanStep("run the tracker's [track] provider; compare with the known versions")]
    steps["Write"] = [PlanStep("render the pinned pkg.toml and write the version directory")]
    if download:
        steps["Fetch"] = [PlanStep("download the new version")]
        steps["Verify"] = [PlanStep("checksum rules of design 9.1")]
        steps["Unpack"] = [PlanStep("unpack into a staged App")]
        steps["Prepare"] = [PlanStep("prepare and rename into place; nothing is activated")]
    steps["Finish"] = [PlanStep("record the result in .gupkg\\state\\track.toml")]
    off = {"Fetch", "Verify", "Unpack", "Prepare"} if not download else set()
    phases = tuple(
        Phase(name, tuple(steps[name]), name in off, ("--no-download" if name in off else ""))
        for name in CHECK_UPDATES_PHASES
    )
    path = view.path or context.package_dir(view.name)
    return InstallPlan(
        "check-updates", view.name, Path(path), context.root, context.mode, None,
        view.installed.version if view.installed else None, None, None, (), phases,
        (), False, {"download": download},
    )
