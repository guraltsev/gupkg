"""Run the operations of the UI programs through the CLI: plan, one confirmation, the checksum policy, grouped execution, one report (design section 13.2).

Neither UI program changes anything itself. An update is planned with ``gupkg update <targets> --plan --json``, the plan is shown and confirmed **once**,
and the ``[manager] checksum_missing`` policy is applied per package (``ask`` asks once per package, ``refuse`` and a non-interactive ``ask`` do not run
the package, ``skip`` relays ``--skip-checksum=setting``, a malformed declared hash is never skippable). The targets then run in one call of the CLI,
which acts in the run's own scope (``gupkg_ui.steps``). The combined result is the end of the log. With ``[cleanup] auto = true`` an update ends with a cleanup (decision D43). Check and update stay separate
operations (D11, D23). One package is a batch of one, so ``gupkg-tui`` and ``gupkg-manager`` share these flows.

Usage and API
-------------
``await run_update(env, rows, ...)``, ``await run_check_updates(env, rows, ...)`` and ``await run_operation(env, command, rows, ...)`` run a batch and return
the ``BatchOutcome``; the screens and the console commands call them with their own ``Sink`` and question callbacks (``Interaction``).

Implementation Approach
-----------------------
Every question is a callback (a dialog in the Textual app, a prompt on the console, ``None`` for a schedule), so the same code serves all of them.
Decisions are made before any call is started and the checksum policy is applied by ``gupkg.model.manager_policy``, never reimplemented here.
A plan that cannot be read, a call that fails and a target that gets no answer are failed targets in the report, never silent successes.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from rich.text import Text

from gupkg.model.checksum import INVALID, MISSING, SKIPPED_SETTING, USER_APPROVED, ChecksumPlan
from gupkg.model.manager_policy import TargetDecision, skip_argument
from .client import ClientError, targets_after_options
from .interaction import Interaction
from .plans import PackageRow, PlannedTarget, decide_targets, read_plan
from .render import literal
from .session import EXIT_INPUT, BatchOutcome, BatchSession, FollowUp, Sink, Step, synthetic_entry
from .startup import UiEnvironment
from .steps import Subject, events_dir_for, make_steps, safe_subjects, subject_of

# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def run_update(
    env: UiEnvironment, rows: Sequence[PackageRow], *, session: BatchSession, sink: Sink, interaction: Interaction, auto_cleanup: bool | None = None,
) -> BatchOutcome:
    """Plan an update of *rows*, confirm it once, apply the checksum policy and run it in one call.

    Parameters
    ----------
    env : UiEnvironment
        The client, the settings and the scope of the run.
    rows : sequence of PackageRow
        The packages to update.
    session : BatchSession
        Runs the calls and writes the verdict.
    sink : Sink
        Where the plan is shown before the question.
    interaction : Interaction
        Who can answer.
    auto_cleanup : bool, optional
        Whether to end with a cleanup; the ``[cleanup] auto`` setting by default.

    Returns
    -------
    BatchOutcome
        The combined result of the whole batch, including targets that were refused, declined or not planned.
    """
    subjects, unsafe = safe_subjects("update", [subject_of(row) for row in rows])

    # Plan: one read-only call that needs no question.
    try:
        document = await env.client.query(targets_after_options("update", ["--plan"], [item.target for item in subjects]))
        planned = read_plan(document, [item.target for item in subjects])
    except ClientError as error:
        entries = [synthetic_entry(item.target, "update", "failed", 3, f"the plan could not be made: {error}") for item in subjects]
        return await session.run([], extra_entries=[*unsafe, *entries])

    # Preview what the policy would do, without answering any question: the targets it would ask about are noted, so the plan can say so.
    policy_of = lambda item: env.settings.checksum_policy  # noqa: E731 - one short rule, used twice below
    would_ask: set[str] = set()
    preview = await decide_targets(
        planned, policy_of, interactive=interaction.interactive, ask=lambda item: would_ask.add(item.target) and False,
    )

    # Show the plan, then ask once for the whole of it (before any per-package question, so a no costs the person nothing).
    entries = [*unsafe, *refused_entries(planned, {key: value for key, value in preview.items() if key not in would_ask})]
    lines, question = plan_lines(planned, preview, would_ask=would_ask)
    sink.lines(lines)
    pending = [item for item in planned if item.state == "ready" and (preview.get(item.target, TargetDecision(False)).run or item.target in would_ask)]
    if pending and interaction.confirm is not None:
        answer = interaction.confirm(lines, question)
        if inspect.isawaitable(answer):
            answer = await answer
        if answer is not True:
            entries += [synthetic_entry(item.target, "update", "cancelled", EXIT_INPUT, "not confirmed; left unchanged", ok=True) for item in pending]
            return await session.run([], extra_entries=entries)

    # Now ask the checksum questions, once per package, and decide every target with the policy.
    decisions = await decide_targets(planned, policy_of, interactive=interaction.interactive, ask=interaction.ask_checksum)
    entries = [*unsafe, *refused_entries(planned, decisions)]
    runnable = [item for item in planned if item.state == "ready" and decisions.get(item.target, TargetDecision(False)).run]

    # Run: one call for every target; the confirmation answers the CLI's own question (--yes).
    by_target = {subject.target: subject for subject in subjects}
    skips = {item.target: decisions[item.target].arguments for item in runnable}
    steps = make_steps("update", [by_target[item.target] for item in runnable], settings=env.settings, options=["--yes"], skips=skips)
    cleanup = env.settings.cleanup_auto if auto_cleanup is None else auto_cleanup
    follow = cleanup_follow_up(env, subjects) if cleanup else None
    return await session.run(steps, extra_entries=entries, follow_up=follow)


def refused_entries(planned: Sequence[PlannedTarget], decisions: dict[str, TargetDecision]) -> list[dict[str, Any]]:
    """Return the entries of the planned targets that will not run: failed plans, refused downloads, declined questions, nothing to do."""
    entries: list[dict[str, Any]] = []
    for item in planned:
        decision = decisions.get(item.target)
        if item.state == "up-to-date":
            entries.append({**synthetic_entry(item.target, "update", "up-to-date", 0, "", ok=True), "version": item.version, "error": None})
        elif decision is not None and not decision.run:
            entries.append(synthetic_entry(item.target, "update", decision.status, EXIT_INPUT, decision.message, ok=True))
        elif item.state == "failed":
            entries.append(synthetic_entry(item.target, "update", "failed", item.exit_code or 3, item.message or "the plan failed"))
    return entries


def plan_lines(
    planned: Sequence[PlannedTarget], decisions: dict[str, TargetDecision], *, would_ask: set[str] = frozenset(),
) -> tuple[list[Text], str]:
    """Return the plan table (one line per target, saying what happens to it) and the one question that confirms it.

    *would_ask* names the targets whose missing checksum the person will be asked about after confirming the plan.
    """
    lines = [Text("Plan", style="bold")]
    count = 0
    for item in planned:
        decision = decisions.get(item.target)
        name = literal(item.package or item.target, limit=60)
        change = f"{literal(item.previous or '-', limit=40)} -> {literal(item.version or '?', limit=40)}"
        if item.state == "up-to-date":
            note, style = "up to date", "dim"
        elif item.target in would_ask:
            note, style = "no checksum: you will be asked", "yellow"
            count += 1
        elif decision is not None and not decision.run:
            note, style = decision.message or decision.status, "yellow"
        elif item.state == "failed":
            note, style = f"cannot be planned: {literal(item.message or item.code or '?', limit=200)}", "red"
        else:
            note, style = _checksum_note(item.checksum, decision), ""
            count += 1
        lines.append(Text(f"  {name:<16} {change:<18} {note}", style=style))
    return lines, f"Update {count or 'no'} package{'s' if count != 1 else ''}?"


def _checksum_note(checksum: ChecksumPlan | None, decision: TargetDecision | None) -> str:
    """Return the words that describe how a target's download will be verified (a skipped checksum is always said)."""
    if checksum is None or checksum.sha256 not in (MISSING, INVALID):
        return "sha256 verified" if checksum is not None else "no download"
    return "NO CHECKSUM: installed once without one" if decision is not None and decision.arguments else "no checksum"


# ---------------------------------------------------------------------------
# Check for updates
# ---------------------------------------------------------------------------


async def run_check_updates(
    env: UiEnvironment, rows: Sequence[PackageRow], *, session: BatchSession, interaction: Interaction, options: Sequence[str] = (),
) -> BatchOutcome:
    """Run ``gupkg check-updates`` for *rows*: it writes and populates new versions and activates nothing.

    The whole batch is one call, in the run's scope. The checksum policy ``skip`` relays ``--skip-checksum=setting``;
    a version the tracker found but whose download has no checksum is reported as refused, and when someone can answer (policy ``ask``) each such
    package is offered once and re-run with ``--skip-checksum=user-approved`` when the answer is yes.
    """
    subjects, unsafe = safe_subjects("check-updates", [subject_of(row) for row in rows])
    skips = {item.target: (skip_argument(SKIPPED_SETTING),) for item in subjects if env.settings.checksum_policy == "skip"}

    # One call carries the whole batch.
    def whole(members: Sequence[Subject], extra: dict[str, tuple[str, ...]]) -> list[Step]:
        """Group *members* for ``check-updates`` with the skip arguments of *extra*."""
        return make_steps("check-updates", members, settings=env.settings, options=["--yes", *options], skips=extra)

    offered: set[str] = set()

    async def retry(outcome: BatchOutcome) -> FollowUp | None:
        """Offer each target refused for a missing checksum once, and re-run the approved ones with the user's approval."""
        approved: list[Subject] = []
        for entry in outcome.entries:
            error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
            subject = next((item for item in subjects if item.target == entry.get("package")), None)
            if error.get("code") != "checksum-missing" or subject is None or subject.target in offered or interaction.ask_checksum is None:
                continue
            offered.add(subject.target)
            candidate = PlannedTarget(
                subject.target, "failed", subject.name, checksum=ChecksumPlan(MISSING, str(error.get("url") or ""), "tracker", error.get("skippable") is True),
                code="checksum-missing",
            )
            decision = (await decide_targets([candidate], lambda item: env.settings.checksum_policy, interactive=True, ask=interaction.ask_checksum)).get(subject.target)
            if decision is not None and decision.run and decision.arguments:
                approved.append(subject)
        if not approved:
            return None
        extra = {item.target: (skip_argument(USER_APPROVED),) for item in approved}
        return FollowUp(tuple(whole(approved, extra)), frozenset(item.name for item in approved))

    return await session.run(whole(subjects, skips), extra_entries=unsafe, follow_up=retry if interaction.interactive else None)


# ---------------------------------------------------------------------------
# Other operations
# ---------------------------------------------------------------------------


async def run_operation(
    env: UiEnvironment, command: str, rows: Sequence[PackageRow], *, session: BatchSession, options: Sequence[str] = (),
    trailing: Sequence[str] = (),
) -> BatchOutcome:
    """Run one more command (``check``, ``install``, ``uninstall``, ``cleanup``, ``switch`` ...) on *rows* in one call.

    The caller has already asked the questions the command needs (the options carry ``--yes``), because the CLI is always called with ``--no-input``.
    """
    subjects, unsafe = safe_subjects(command, [subject_of(row) for row in rows])
    steps = make_steps(command, subjects, settings=env.settings, options=options, trailing=trailing)
    return await session.run(steps, extra_entries=unsafe)


def cleanup_follow_up(env: UiEnvironment, subjects: Sequence[Subject]) -> Callable[[BatchOutcome], Awaitable[FollowUp | None]]:
    """Return the follow-up that ends an update with a cleanup of the run's root when a package was updated (never after a cancel).

    A run has one scope, so the cleanup is one call, made only when something was updated.
    """
    made: list[bool] = []

    async def follow(outcome: BatchOutcome) -> FollowUp | None:
        """Return the cleanup step once, when at least one package was updated."""
        if made:
            return None
        made.append(True)
        if not subjects or not any(entry.get("ok") is True and entry.get("status") == "updated" for entry in outcome.entries):
            return None
        step = Step("cleanup of the root", targets_after_options("cleanup", ["--yes"], []), (), events_dir_for(subjects[0], env.settings))
        return FollowUp((step,))
    return follow
