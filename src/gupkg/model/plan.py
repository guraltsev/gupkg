"""Describe what an operation will do as immutable data: requests, effects, phases, plans and results.

An ``InstallPlan`` is the whole answer to "what would ``gupkg install firefox`` do?": the
chosen version, the install mode, whether the population of ``App`` is needed and where its
source comes from, and the integration effects to add, update and remove. The planner builds
it from a package view without touching the machine; the executor runs it; ``--plan --json``
prints ``InstallPlan.to_data()``. The module also defines the typed results the lifecycle
entry points return, so front ends render them and never parse text.

Usage and API
-------------
Build a ``Request`` per target, read ``InstallPlan`` fields or ``plan.to_data()``, and read
``PackageResult`` and ``BatchResult`` for the outcome. ``effect_key(effect)`` names the identity
of an effect, so two effects with the same key but different fields are an update. ``EFFECT_KINDS``
lists the kinds the install record carries.

Implementation Approach
-----------------------
Everything is a frozen dataclass of plain values. An effect is the ``Effect`` of ``gupkg.model.state``
(the install record's own type), so what the plan declares is exactly what the record stores and
what ``check`` verifies. Effect fields are strings (and booleans) only, so they survive a round trip
through TOML unchanged. The module does no input or output.

Effect kinds and their fields (all paths absolute):

``shortcut``
    ``path`` (the ``.lnk``), ``target``, ``arguments``, ``working_dir``, ``icon_location``, ``description``, and
    ``run_as_admin`` (present, as ``true``, only for a shortcut that asks for the UAC prompt).
``shim``
    ``path`` (the launcher ``.exe``), ``config`` (its ``.config.toml``), ``type``, ``linkage``, ``target``,
    ``arguments``, ``forward_args``, ``elevate``, ``working_dir`` and ``config_sha256``.
``file``
    ``path``, ``content`` and ``sha256`` of a content wrapper (a ``.cmd`` shim).
``env``
    ``scope`` (``system`` or ``user``), ``name``, ``value``.
``path``
    ``scope``, ``entry`` and ``added`` (this install put the entry on ``PATH``); ``shared`` marks the
    ``bin`` folder, which belongs to the mode and is never removed by one package.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..redact import redact_text, redact_url
from .checksum import ChecksumPlan
from .manifest import INSTALLER_LABEL
from .state import Effect

OPERATIONS = ("install", "update", "switch", "uninstall", "cleanup", "repair", "check-updates", "check", "link")

# The phases of an install-like plan, in order (design 13.1), and those of ``check-updates``.
INSTALL_PHASES = ("Resolve", "Fetch", "Verify", "Unpack", "Prepare", "Activate", "Integrate", "Finish")
CHECK_UPDATES_PHASES = ("Resolve", "Track", "Write", "Fetch", "Verify", "Unpack", "Prepare", "Finish")

# The kinds of effect the install record carries and ``check`` verifies.
EFFECT_KINDS = ("shortcut", "shim", "file", "env", "path")

# Exit codes of design 10.1 that results carry.
EXIT_OK = 0
EXIT_USER = 2
EXIT_FAILED = 3
EXIT_INTERNAL = 4


EXIT_INPUT = 5
EXIT_NEWER_GUPKG = 6


@dataclass(frozen=True)
class Request:
    """One operation on one target.

    Attributes
    ----------
    operation : str
        One of ``OPERATIONS``.
    selector : str
        The target as the user wrote it (``name``, ``name@version``, a path).
    version : str, optional
        The version ``switch`` activates.
    refresh : bool
        ``install --refresh``: rebuild an ``App`` that can be rebuilt.
    purge : bool
        ``uninstall --purge``: also delete the package root and its data.
    keep : int
        ``cleanup --keep N``: how many non-current ``App`` folders stay (default one previous).
    bundled : bool
        ``cleanup --bundled``: drop bundled gupkg copies where a launcher would find a gupkg through gupkg-location or PATH.
    no_download : bool
        ``check-updates --no-download``: write the version directory only.
    skip : str, optional
        The decision of ``--skip-checksum`` (``gupkg.model.checksum.parse_skip_flag``).
    fix : bool
        ``check --fix``: re-apply integration and rebuild a missing ``App``.
    download_only : bool
        ``update`` that populates the chosen version's ``App`` and stops: nothing is activated or integrated.
    sweep : bool
        ``cleanup`` over every package of a root rather than the packages the user named: a package that another run holds is
        left alone (status ``locked``, not a failure). A package the user names that is busy fails with ``package-busy``.
    link_to : str, optional
        ``link``: the ``registry/name`` entry the package follows from now on (without it the package follows the entry with
        its own name again).
    unlink : bool
        ``link --unlink``: the package follows no registry entry.
    """

    operation: str
    selector: str
    version: str | None = None
    refresh: bool = False
    purge: bool = False
    keep: int = 1
    bundled: bool = False
    no_download: bool = False
    skip: str | None = None
    fix: bool = False
    download_only: bool = False
    sweep: bool = False
    link_to: str | None = None
    unlink: bool = False

    def __post_init__(self) -> None:
        """Refuse an operation the engine does not know."""
        if self.operation not in OPERATIONS:
            raise ValueError(f"unknown operation {self.operation!r}; expected one of {', '.join(OPERATIONS)}")
        if isinstance(self.keep, bool) or not isinstance(self.keep, int) or self.keep < 0:
            raise ValueError("keep must be a whole number of zero or more")


def effect_key(effect: Effect) -> tuple[str, ...]:
    """Return what identifies *effect*: same key and different fields means the effect is an update.

    A shortcut, shim or content file is identified by its path (letter case ignored), an environment
    value by scope and name, a ``PATH`` entry by scope and normalised entry.
    """
    fields = effect.fields
    if effect.kind in ("env",):
        return (effect.kind, str(fields.get("scope", "")), str(fields.get("name", "")).casefold())
    if effect.kind == "path":
        return (effect.kind, str(fields.get("scope", "")), normalize_entry(str(fields.get("entry", ""))))
    return (effect.kind, os.path.normcase(os.path.normpath(str(fields.get("path", "")))))


def normalize_entry(entry: str) -> str:
    """Return a ``PATH`` entry in the form used to compare entries: normalised, no trailing separator, lower case."""
    return os.path.normcase(os.path.normpath(entry)).rstrip("\\/")


@dataclass(frozen=True)
class PlannedEffect:
    """One effect of a plan and what the executor does with it.

    ``action`` is ``add`` (not in the install record), ``update`` (in the record with other fields),
    ``keep`` (identical) or ``remove`` (in the record, no longer wanted). ``previous`` holds the
    recorded effect of an ``update`` or ``remove``.
    """

    action: str
    effect: Effect
    previous: Effect | None = None

    def to_data(self) -> dict[str, Any]:
        """Return the JSON-ready description used by ``--plan --json``."""
        return {"action": self.action, "kind": self.effect.kind, **{key: value for key, value in self.effect.fields.items()}}


@dataclass(frozen=True)
class PlanStep:
    """One readable line of a phase; ``kind`` is ``info``, ``add``, ``remove`` or ``ok`` (the event step kinds)."""

    text: str
    kind: str = "info"


@dataclass(frozen=True)
class Phase:
    """One phase of a plan with its steps; a skipped phase says why."""

    name: str
    steps: tuple[PlanStep, ...] = ()
    skipped: bool = False
    reason: str = ""

    def to_data(self) -> dict[str, Any]:
        """Return the JSON-ready description of the phase."""
        data: dict[str, Any] = {"name": self.name, "skipped": self.skipped, "steps": [redact_text(step.text) for step in self.steps]}
        if self.reason:
            data["reason"] = self.reason
        return data


@dataclass(frozen=True)
class PopulatePlan:
    """How the chosen version's ``App`` is obtained, and whether anything has to happen.

    Attributes
    ----------
    kind : str
        ``download``, ``git``, ``script`` or ``shipped`` (the ``[source]`` kind).
    needed : bool
        Whether ``App`` is built now: it is missing or empty, or ``--refresh`` rebuilds a recoverable one.
    reason : str
        Why it is (not) needed, for the Resolve phase and the plan output.
    url : str, optional
        The address the built-in download script fetches (the only one: design section 4).
    checksum : ChecksumPlan, optional
        What ``--plan`` reports for the download (``present`` or ``missing``; a malformed declared hash makes the version invalid, so no plan exists).
    script : str, optional
        The custom populate script.
    recoverable : bool
        Whether a missing ``App`` can be rebuilt (a download URL, a git source or a script).
    installer : tuple of str, optional
        The ``[source.installer] run`` arguments when the version runs a vendor installer (labelled ``runs vendor installer``).
    """

    kind: str
    needed: bool
    reason: str
    url: str | None = None
    checksum: ChecksumPlan | None = None
    script: str | None = None
    recoverable: bool = False
    installer: tuple[str, ...] | None = None

    def to_data(self) -> dict[str, Any]:
        """Return the JSON-ready description of the population."""
        data: dict[str, Any] = {"kind": self.kind, "needed": self.needed, "reason": self.reason, "recoverable": self.recoverable}
        if self.installer is not None:
            data["label"] = INSTALLER_LABEL
            data["installer"] = list(self.installer)
        if self.url is not None:
            data["url"] = redact_url(self.url)
        if self.checksum is not None:
            data["checksum"] = self.checksum.to_data()
        if self.script is not None:
            data["script"] = self.script
        return data


@dataclass(frozen=True)
class CopyIn:
    """A version directory that is copied into the package root before the install.

    ``origin`` is ``registry`` (a pinned version of a followed registry), ``distribution`` (a folder
    outside every root, design 21.4) or ``tracker`` (the registry's tracker alone).
    """

    origin: str
    source: Path
    registry: str | None = None
    fingerprint: str | None = None

    def to_data(self) -> dict[str, Any]:
        """Return the JSON-ready description of the copy."""
        data: dict[str, Any] = {"origin": self.origin, "source": str(self.source)}
        if self.registry is not None:
            data["registry"] = self.registry
        return data


@dataclass(frozen=True)
class InstallPlan:
    """What one operation on one package will do, before it does anything.

    Attributes
    ----------
    operation : str
        The operation the plan serves (``install``, ``update``, ``switch``, ``uninstall``...).
    package : str
        The package name.
    package_path : Path
        The package root (it exists only after a copy-in for a registry-only package).
    root : Path
        The root that holds the package.
    mode : str
        ``system`` or ``user``.
    version : str, optional
        The version the plan activates (or, for ``uninstall``, the active one).
    previous : str, optional
        The version active now, if any.
    populate : PopulatePlan, optional
        The population of ``App``; ``None`` for operations that never populate.
    copy_in : CopyIn, optional
        A version directory to copy into the package root first.
    effects : tuple of PlannedEffect
        The integration diff against the install record.
    phases : tuple of Phase
        The phases in order, each with readable steps.
    notes : tuple of str
        Facts the plan reports (origin label, diverged versions).
    purge : bool
        ``uninstall --purge``.
    """

    operation: str
    package: str
    package_path: Path
    root: Path
    mode: str
    version: str | None = None
    previous: str | None = None
    populate: PopulatePlan | None = None
    copy_in: CopyIn | None = None
    effects: tuple[PlannedEffect, ...] = ()
    phases: tuple[Phase, ...] = ()
    notes: tuple[str, ...] = ()
    purge: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def changes(self) -> tuple[PlannedEffect, ...]:
        """Return the effects that change the machine (everything except ``keep``)."""
        return tuple(item for item in self.effects if item.action != "keep")

    def to_data(self) -> dict[str, Any]:
        """Return the JSON-ready plan document of ``--plan --json`` (design 10.4).

        Per target: the chosen version, the mode, ``sha256: present | missing``, the source URL, and the effects to add or remove.
        """
        data: dict[str, Any] = {
            "operation": self.operation,
            "package": self.package,
            "path": str(self.package_path),
            "mode": self.mode,
            "version": self.version,
            "previous": self.previous,
            "effects": [item.to_data() for item in self.effects if item.action != "keep"],
            "phases": [item.to_data() for item in self.phases],
            "notes": [redact_text(note) for note in self.notes],
        }
        if self.populate is not None:
            data["populate"] = self.populate.to_data()
            if self.populate.checksum is not None:
                data["sha256"] = self.populate.checksum.sha256
                data["url"] = redact_url(self.populate.checksum.url)
            elif self.populate.url is not None:
                data["url"] = redact_url(self.populate.url)
        if self.copy_in is not None:
            data["copy_in"] = self.copy_in.to_data()
        if self.purge:
            data["purge"] = True
        data.update(dict(self.extra))
        return data


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageResult:
    """The typed outcome of one operation on one package.

    Attributes
    ----------
    package : str
        The package name or the target text when it could not be resolved.
    operation : str
        The operation.
    ok : bool
        Whether it succeeded.
    status : str
        ``installed``, ``repaired``, ``updated``, ``up-to-date``, ``switched``, ``uninstalled``,
        ``purged``, ``cleaned``, ``checked``, ``new-version``, ``no-new-version``, ``skipped`` or ``failed``.
    version : str, optional
        The version now active (or found, for ``check-updates``).
    previous : str, optional
        The version that was active before.
    mode : str, optional
        The install mode.
    exit_code : int
        0, 2 (user or configuration error), 3 (operation failed), 4 (internal) or 5 (declined, input needed).
    error : dict, optional
        A structured error (``code``, ``message``, ``hint`` and what the error carries, such as ``url``).
    receipt : str, optional
        The receipt file written.
    log : str, optional
        The package's log file for the run.
    warnings : tuple of str
        Problems that did not stop the work.
    details : dict
        Operation-specific facts (applied effects, versions removed, problems found by ``check``...).
    """

    package: str
    operation: str
    ok: bool
    status: str
    version: str | None = None
    previous: str | None = None
    mode: str | None = None
    exit_code: int = EXIT_OK
    error: Mapping[str, Any] | None = None
    receipt: str | None = None
    log: str | None = None
    warnings: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, Any]:
        """Return the per-target entry of the ``--json`` result document."""
        data: dict[str, Any] = {
            "package": self.package, "operation": self.operation, "ok": self.ok, "status": self.status,
            "version": self.version, "previous": self.previous, "mode": self.mode, "exit_code": self.exit_code,
            "receipt": self.receipt, "log": self.log, "warnings": [redact_text(item) for item in self.warnings],
        }
        if self.error is not None:
            data["error"] = dict(self.error)
        if self.details:
            data["details"] = dict(self.details)
        return data

    def summary(self) -> str:
        """Return one line that says what happened, for the closing summary."""
        if not self.ok:
            reason = (self.error or {}).get("message", self.status)
            return f"x {self.package}: {self.operation} failed: {reason}"
        if self.status == "skipped":
            return f"- {self.package}: skipped"
        if self.status == "locked":
            return f"- {self.package}: left alone, another gupkg run is changing it"
        version = f" {self.version}" if self.version else ""
        was = f" (was {self.previous})" if self.previous and self.previous != self.version else ""
        return f"ok {self.package}{version}: {self.status}{was}"


@dataclass(frozen=True)
class BatchResult:
    """The outcome of a call with several targets: one ``PackageResult`` per target."""

    results: tuple[PackageResult, ...] = ()

    @property
    def ok(self) -> bool:
        """Return whether every target succeeded (a skipped one counts as not done)."""
        return all(item.ok and item.status != "skipped" for item in self.results)

    @property
    def exit_code(self) -> int:
        """Return the call's exit code: zero when all succeeded, else the highest code of a failure."""
        failed = [item.exit_code or EXIT_FAILED for item in self.results if not item.ok]
        if failed:
            return max(failed)
        return EXIT_INPUT if any(item.status == "skipped" for item in self.results) else EXIT_OK

    def to_data(self) -> dict[str, Any]:
        """Return the ``--json`` result document of the call."""
        return {"ok": self.ok, "exit_code": self.exit_code,
                "results": [item.to_data() for item in self.results]}

    def summary(self) -> str:
        """Return the closing summary: one line per target, naming every failure."""
        lines = [item.summary() for item in self.results]
        failed = [item.package for item in self.results if not item.ok]
        skipped = [item.package for item in self.results if item.status == "skipped"]
        if failed:
            lines.append(f"{len(failed)} of {len(self.results)} failed: {', '.join(failed)}")
        locked = [item.package for item in self.results if item.status == "locked"]
        if skipped:
            lines.append(f"Skipped after cancel: {', '.join(skipped)}")
        if locked:
            lines.append(f"Left alone (in use by another run): {', '.join(locked)}")
        return "\n".join(lines)


def split_effects(effects: Sequence[PlannedEffect]) -> dict[str, list[PlannedEffect]]:
    """Group planned effects by their action."""
    groups: dict[str, list[PlannedEffect]] = {"add": [], "update": [], "keep": [], "remove": []}
    for item in effects:
        groups[item.action].append(item)
    return groups
