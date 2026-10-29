"""Turn the typed plan document into the targets the UI decides on, and decide each planned target with the manager's checksum policy.

The UI programs ask the CLI what it would do (``gupkg update <targets> --plan --json``). Reading the document is the job of
``gupkg.report.documents`` (one reader, strict: a document it cannot understand raises ``DocumentError``, a malformed ``sha256`` marker never
means "present"); this module only maps its entries to ``PlannedTarget`` (a failed entry with the stable error code ``invalid-checksum`` becomes an
``invalid`` checksum plan that no policy ever skips) and feeds the policy of ``gupkg.model.manager_policy`` (issue 027), putting the questions to the person.
``PackageRow`` is the typed list row of the same reader, with the target the UI passes to the CLI.

Usage and API
-------------
``PackageRow.of(row)`` makes the UI row of a ``ListRow``. ``read_plan(document, targets)`` returns one ``PlannedTarget`` per target and raises
``DocumentError`` for a document that cannot be read. ``await decide_targets(planned, policy_of, interactive=..., ask=...)`` returns the
``TargetDecision`` of every planned target, asking at most once per package through ``ask`` (a coroutine function or a plain function).

Implementation Approach
-----------------------
Deciding calls ``decide_target`` of ``manager_policy`` twice at most per target: first with an ``ask`` that only
notes that a question is needed, then, after the person answered, with that answer, so the rules of the policy table are written once.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from gupkg.model.checksum import INVALID, ChecksumPlan
from gupkg.model.manager_policy import TargetDecision, decide_target
from gupkg.report.documents import ListRow
from gupkg.report.documents import read_plan as read_plan_document

from .render import literal

# The error code that a malformed declared hash gives (design 10.4): never skippable.
INVALID_CHECKSUM_CODE = "invalid-checksum"


@dataclass(frozen=True)
class PackageRow(ListRow):
    """One package of the inventory: a ``ListRow`` of ``gupkg.report.documents`` plus the target the UI hands to the CLI.

    Attributes
    ----------
    target_text : str, optional
        A target to use instead of the name, such as a folder the person named.
    """

    target_text: str | None = None

    @classmethod
    def of(cls, row: ListRow) -> PackageRow:
        """Return the UI row of *row*, its warnings cleaned for display (they come from packages and the network)."""
        return cls(
            row.name, row.status, row.version, row.newest_known, row.path, row.following, tuple(literal(text) for text in row.warnings),
            row.restriction, row.restriction_reason, row.problems,
        )

    @property
    def target(self) -> str:
        """The target the CLI understands: the package name (a run has one root, so a name is never ambiguous) unless the person named a folder."""
        if self.target_text is not None:
            return self.target_text
        return self.name


@dataclass(frozen=True)
class PlannedTarget:
    """What the plan says about one target.

    Attributes
    ----------
    target : str
        The target as it was given to ``--plan``.
    state : str
        ``ready`` (a plan exists), ``up-to-date`` (nothing to do) or ``failed`` (no plan; see ``code`` and ``message``).
    package, version, previous : str or None
        What the plan reports.
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
    version: str | None = None
    previous: str | None = None
    checksum: ChecksumPlan | None = None
    code: str | None = None
    message: str | None = None
    exit_code: int = 0
    notes: tuple[str, ...] = ()


def read_plan(document: object, targets: Sequence[str]) -> list[PlannedTarget]:
    """Return one ``PlannedTarget`` per requested target, in order; a target the document does not answer for has failed.

    Parameters
    ----------
    document : object
        The parsed ``--plan --json`` document.
    targets : sequence of str
        The targets that were planned.

    Raises
    ------
    gupkg.report.documents.DocumentError
        If the document cannot be read (nothing is then planned).
    """
    by_target = {item.target: item for item in read_plan_document(document).items}
    planned: list[PlannedTarget] = []
    for target in targets:
        if target not in by_target:
            planned.append(PlannedTarget(target, "failed", code="no-plan", message="the plan has no entry for this target", exit_code=4))
            continue
        item = by_target[target]
        if item.plan is not None:
            plan = item.plan
            planned.append(PlannedTarget(
                target, "ready", plan.package, plan.version, plan.previous, plan.checksum, None, None, 0, tuple(literal(note) for note in plan.notes),
            ))
        elif item.ok and item.result is not None and item.result.status == "up-to-date":
            planned.append(PlannedTarget(target, "up-to-date", item.result.package, item.result.version))
        else:
            # No plan: a malformed declared hash is its own never-skippable state, anything else failed.
            result = item.result
            error = result.error if result is not None else None
            package = result.package if result is not None else None
            if error is not None and error.code == INVALID_CHECKSUM_CODE:
                planned.append(PlannedTarget(
                    target, "failed", package, checksum=ChecksumPlan(INVALID, "", "", False), code=INVALID_CHECKSUM_CODE,
                    message=error.message, exit_code=item.exit_code or 2,
                ))
            else:
                planned.append(PlannedTarget(
                    target, "failed", package, code=(error.code if error is not None else None) or "failed",
                    message=(error.message if error is not None else None) or "the plan failed", exit_code=item.exit_code or 3,
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
        Returns ``ask``, ``refuse`` or ``skip`` for a target.
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
