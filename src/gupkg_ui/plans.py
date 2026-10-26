"""Read ``--plan --json`` documents and the package list into typed rows, and decide each planned target with the manager's checksum policy.

The UI programs ask the CLI what it would do (``gupkg update <targets> --plan --json``) and what exists (``gupkg list --json``);
both documents are untrusted input, so they are read strictly. A plan entry that cannot be understood is a target that is not
run, a malformed ``sha256`` marker never means "present", and a failed entry with the stable error code ``invalid-checksum`` becomes
an ``invalid`` checksum plan that no policy ever skips. The policy itself lives in ``gupkg.model.manager_policy`` (issue 027); this
module only feeds it from the documents and puts the questions to the person.

Usage and API
-------------
``read_packages(document)`` returns the ``PackageRow`` list of a ``list`` document. ``read_plan(document, targets)`` returns one
``PlannedTarget`` per target. ``await decide_targets(planned, policy_of, interactive=..., ask=...)`` returns the
``TargetDecision`` of every planned target, asking at most once per package through ``ask`` (a coroutine function or a plain
function).

Implementation Approach
-----------------------
Parsing never trusts a type: names and paths must be text, flags must be booleans, and the ``sha256`` marker must be one of the
documented words. Deciding calls ``decide_target`` of ``manager_policy`` twice at most per target: first with an ``ask`` that only
notes that a question is needed, then, after the person answered, with that answer, so the rules of the policy table are written once.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gupkg.model.checksum import INVALID, MISSING, PRESENT, ChecksumPlan
from gupkg.model.manager_policy import TargetDecision, decide_target

from .render import literal

# The error code that a malformed declared hash gives (design 10.4): never skippable.
INVALID_CHECKSUM_CODE = "invalid-checksum"


@dataclass(frozen=True)
class PackageRow:
    """One package of the inventory (a row of ``gupkg list --json``).

    Attributes
    ----------
    name, mode, status : str
        Identity and derived status (``installed``, ``outdated``, ``broken``, ``local``, ``available`` ...).
    version, newest_known, path, following : str or None
        The active version, the newest version an update would activate, the package folder and the followed registry entry.
    warnings : tuple of str
        What the catalog warns about for this package.
    target_text : str, optional
        A target to use instead of ``mode:name``, such as a folder the person named (an unmanaged version directory has no mode yet).
    """

    name: str
    mode: str
    status: str
    version: str | None
    newest_known: str | None
    path: str | None
    following: str | None
    warnings: tuple[str, ...] = ()
    target_text: str | None = None

    @property
    def target(self) -> str:
        """The target the CLI understands: ``user:alpha`` (a bare name may be ambiguous between roots) unless the person named a folder."""
        if self.target_text is not None:
            return self.target_text
        return f"{self.mode}:{self.name}" if self.mode else self.name


@dataclass(frozen=True)
class PlannedTarget:
    """What the plan says about one target.

    Attributes
    ----------
    target : str
        The target as it was given to ``--plan``.
    state : str
        ``ready`` (a plan exists), ``up-to-date`` (nothing to do) or ``failed`` (no plan; see ``code`` and ``message``).
    package, mode, version, previous : str or None
        What the plan reports.
    requires_elevation : bool
        Whether running it needs the administrator token.
    checksum : ChecksumPlan or None
        The download's checksum status, ``None`` when the plan downloads nothing.
    code, message : str or None
        For a failed target, the stable error code and the message of the CLI.
    exit_code : int
        The exit code of the target's plan entry.
    notes : tuple of str
        The plan's notes.
    """

    target: str
    state: str
    package: str | None = None
    mode: str | None = None
    version: str | None = None
    previous: str | None = None
    requires_elevation: bool = False
    checksum: ChecksumPlan | None = None
    code: str | None = None
    message: str | None = None
    exit_code: int = 0
    notes: tuple[str, ...] = ()


def _text(value: Any) -> str | None:
    """Return *value* when it is text, else ``None``."""
    return value if isinstance(value, str) else None


def read_packages(document: Mapping[str, Any]) -> list[PackageRow]:
    """Return the rows of a ``list`` document; a row without a text name and status, or without a mode unless it is ``available``, is left out (it cannot be acted on).

    A registry package that is not installed (``available``) has no mode yet: the CLI gives ``null`` and the row's mode is empty.
    """
    rows: list[PackageRow] = []
    for item in document.get("packages", ()):
        if not isinstance(item, Mapping):
            continue
        name, status = _text(item.get("name")), _text(item.get("status"))
        mode = _text(item.get("mode")) or ("" if status == "available" else None)
        if not name or mode is None or not status:
            continue
        warnings = tuple(literal(text) for text in item.get("warnings", ()) if isinstance(text, str))
        rows.append(PackageRow(
            name, mode, status, _text(item.get("version")), _text(item.get("newest_known")), _text(item.get("path")),
            _text(item.get("following")), warnings,
        ))
    return rows


@dataclass(frozen=True)
class PackageInfo:
    """Everything ``gupkg info --json`` knows about one package, read strictly for the package view.

    Attributes
    ----------
    row : PackageRow
        Identity, status and versions.
    versions : tuple of tuple
        ``(name, status, problems, label)`` of every version directory in the package root.
    registry_versions : tuple of str
        The versions the followed registry entry pins.
    tracker : tuple or None
        ``(valid, problems)`` of the local tracker, ``None`` when there is none.
    link : tuple or None
        ``(registry, name, unlinked)`` when the package is linked to a registry entry.
    notes : tuple of str
        What verification and the receipt say (``unverified``, ``self-reported`` ...), each as ``kind: message``.
    receipts, problems : tuple of str
        The versions that have a receipt; the reasons the package is broken.
    log, last_check : str or None
        The log of the last install and the summary of the last ``check-updates``.
    """

    row: PackageRow
    versions: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = ()
    registry_versions: tuple[str, ...] = ()
    tracker: tuple[bool, tuple[str, ...]] | None = None
    link: tuple[str, str, bool] | None = None
    notes: tuple[str, ...] = ()
    receipts: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    log: str | None = None
    last_check: str | None = None
    diverged: tuple[str, ...] = ()


def _texts(value: Any) -> tuple[str, ...]:
    """Return the text items of a list value (anything else is ignored)."""
    return tuple(literal(item) for item in value if isinstance(item, str)) if isinstance(value, (list, tuple)) else ()


def read_info(document: Mapping[str, Any]) -> PackageInfo | None:
    """Return the first package of an ``info`` document, or ``None`` when the document holds no readable package."""
    rows = read_packages(document)
    raw = next((item for item in document.get("packages", ()) if isinstance(item, Mapping)), None)
    if not rows or raw is None:
        return None
    versions = tuple(
        (literal(item.get("name", "?"), limit=80), literal(item.get("status", "?"), limit=40), _texts(item.get("problems")), _text(item.get("label")))
        for item in raw.get("versions", ()) if isinstance(item, Mapping)
    )
    tracker = raw.get("tracker")
    link = raw.get("link")
    notes = tuple(
        f"{literal(item.get('kind', '?'), limit=40)}: {literal(item.get('message', ''))}" for item in raw.get("notes", ()) if isinstance(item, Mapping)
    )
    check = raw.get("last_check")
    return PackageInfo(
        rows[0], versions, _texts(raw.get("registry_versions")),
        (tracker.get("valid") is True, _texts(tracker.get("problems"))) if isinstance(tracker, Mapping) else None,
        (_text(link.get("registry")) or "?", _text(link.get("name")) or "?", link.get("unlinked") is True) if isinstance(link, Mapping) else None,
        notes, _texts(raw.get("receipts")), _texts(raw.get("problems")), _text(raw.get("log")),
        literal(check) if isinstance(check, (str, Mapping)) else None, _texts(raw.get("diverged")),
    )


def read_checksum(plan: Mapping[str, Any]) -> ChecksumPlan | None:
    """Return the checksum plan of a plan entry, ``None`` when it downloads nothing; an unreadable marker raises ``ValueError``."""
    populate = plan.get("populate")
    data = populate.get("checksum") if isinstance(populate, Mapping) else None
    if data is None:
        return None
    if not isinstance(data, Mapping) or data.get("sha256") not in (PRESENT, MISSING, INVALID) or not isinstance(data.get("skippable"), bool):
        raise ValueError("the checksum status of the plan is not understood")
    return ChecksumPlan(data["sha256"], _text(data.get("url")) or "", _text(data.get("url_from")) or "", data["skippable"])


def read_plan(document: Mapping[str, Any], targets: Sequence[str]) -> list[PlannedTarget]:
    """Return one ``PlannedTarget`` per requested target, in order; a target the document does not answer for has failed.

    Parameters
    ----------
    document : mapping
        The checked ``--plan --json`` document.
    targets : sequence of str
        The targets that were planned.
    """
    by_target = {entry.get("target"): entry for entry in document.get("plans", ()) if isinstance(entry, Mapping)}
    planned: list[PlannedTarget] = []
    for target in targets:
        entry = by_target.get(target)
        if entry is None:
            planned.append(PlannedTarget(target, "failed", code="no-plan", message="the plan has no entry for this target", exit_code=4))
            continue
        exit_code = entry.get("exit_code") if type(entry.get("exit_code")) is int else 4

        # A plan exists: read the facts the person needs and the checksum status the policy needs.
        if entry.get("ok") is True and isinstance(entry.get("plan"), Mapping):
            plan = entry["plan"]
            try:
                checksum = read_checksum(plan)
            except ValueError as error:
                planned.append(PlannedTarget(target, "failed", package=_text(plan.get("package")), code="unreadable-plan", message=str(error), exit_code=4))
                continue
            planned.append(PlannedTarget(
                target, "ready", _text(plan.get("package")), _text(plan.get("mode")), _text(plan.get("version")),
                _text(plan.get("previous")), plan.get("requires_elevation") is True, checksum, None, None, 0,
                tuple(literal(note) for note in plan.get("notes", ()) if isinstance(note, str)),
            ))
            continue

        # No plan: "up to date" is fine, a malformed declared hash is its own never-skippable state, anything else failed.
        result = entry.get("result") if isinstance(entry.get("result"), Mapping) else {}
        error = result.get("error") if isinstance(result.get("error"), Mapping) else {}
        package = _text(result.get("package"))
        if entry.get("ok") is True and result.get("status") == "up-to-date":
            planned.append(PlannedTarget(target, "up-to-date", package, _text(result.get("mode")), _text(result.get("version"))))
        elif error.get("code") == INVALID_CHECKSUM_CODE:
            planned.append(PlannedTarget(
                target, "failed", package, _text(result.get("mode")), checksum=ChecksumPlan(INVALID, "", "", False),
                code=INVALID_CHECKSUM_CODE, message=_text(error.get("message")), exit_code=exit_code or 2,
            ))
        else:
            planned.append(PlannedTarget(
                target, "failed", package, _text(result.get("mode")), code=_text(error.get("code")) or "failed",
                message=_text(error.get("message")) or "the plan failed", exit_code=exit_code or 3,
            ))
    return planned


async def decide_targets(
    planned: Sequence[PlannedTarget],
    policy_of: Callable[[PlannedTarget], str],
    *,
    interactive: bool,
    ask: Callable[[PlannedTarget], bool | Awaitable[bool]] | None,
) -> dict[str, TargetDecision]:
    """Decide every ready or checksum-invalid target with ``manager_policy``, asking each person-answerable question once.

    Parameters
    ----------
    planned : sequence of PlannedTarget
        The targets from ``read_plan``.
    policy_of : callable
        Returns ``ask``, ``refuse`` or ``skip`` for a target (it may differ between user and system targets).
    interactive : bool
        Whether someone can answer. ``False`` makes ``ask`` behave as ``refuse`` (a schedule, ``--yes``, no terminal).
    ask : callable, optional
        Shows the package, version, URL and its source and returns ``True`` to install once without a checksum. May be a
        coroutine function.

    Returns
    -------
    dict
        Target to ``TargetDecision``; a target that is up to date or failed without a checksum issue has no entry.
    """
    decisions: dict[str, TargetDecision] = {}
    for item in planned:
        if item.state == "up-to-date" or (item.state == "failed" and item.checksum is None):
            continue
        if item.checksum is None:
            decisions[item.target] = TargetDecision(True)
            continue

        # First pass: let the policy say whether it needs a person; then ask exactly once and apply the answer.
        wanted: list[bool] = []
        policy = policy_of(item)
        decision = decide_target(item.checksum, policy, interactive=interactive, ask=lambda _plan: wanted.append(True) or False)
        if wanted and ask is not None:
            answer = ask(item)
            if inspect.isawaitable(answer):
                answer = await answer
            decision = decide_target(item.checksum, policy, interactive=interactive, ask=lambda _plan, said=answer is True: said)
        decisions[item.target] = decision
    return decisions
