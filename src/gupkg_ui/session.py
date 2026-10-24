"""Run the CLI calls of one batch in order, show their events as they arrive, and combine what they report.

A batch is a list of ``Step`` objects, each one ``gupkg`` call (for example one non-elevated ``update`` call for the user targets and
one call for the system targets, which the CLI runs in a single elevated child). ``BatchSession.run`` starts them one after another,
follows each call's events file, hands the styled lines to a ``Sink`` while the call runs, and returns a ``BatchOutcome`` with one result
entry per target. The outcome fails closed: a call that could not be started, whose result cannot be read, whose events file lacks
``run_end`` or that returns no entry for a target makes that target a failed one, never an ``ok`` one. Cancelling (decision D37) asks
the running call to stop after its current package through the stop marker and starts no further call; the targets that never ran are
reported as skipped.

Usage and API
-------------
Build ``BatchSession(client, sink)``, call ``await session.run(steps, extra_entries=...)`` and read the ``BatchOutcome``;
``session.request_cancel()`` may be called from the UI while ``run`` is awaited. ``Sink`` is the three-method protocol that a
screen or the console implements. ``exit_code_of(entries, problems)`` is the one place that turns results into the combined exit code
(the highest code of a failure; a refused target is 3, a skipped or declined one 5).

Implementation Approach
-----------------------
The session owns no widget: it feeds an ``EventRenderer`` and writes to the sink, so the same code runs under Textual and in the
non-interactive commands. All waiting is ``await``: the UI's event loop is never blocked by a child process.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from rich.text import Text

from .client import CallResult, Client, StopRequest
from .render import EventRenderer, literal, summarize_entries, verdict_lines

# Exit codes of the interface (design 10.1) that the UI programs also use for what they decide themselves.
EXIT_OK, EXIT_USAGE, EXIT_FAILED, EXIT_INTERNAL, EXIT_INPUT = 0, 2, 3, 4, 5


class Sink(Protocol):
    """Receive what a running batch shows: styled lines, the live progress text and a one-line status."""

    def lines(self, lines: Sequence[Text]) -> None:
        """Append lasting lines to the log."""

    def progress(self, text: str | None) -> None:
        """Show (or, with ``None``, clear) the live progress line."""

    def status(self, text: str) -> None:
        """Show the one-line run state."""


@dataclass(frozen=True)
class Step:
    """One ``gupkg`` call of a batch.

    Attributes
    ----------
    label : str
        What the call is for, shown above its output (``user targets``).
    args : tuple of str
        The command word, its options and, after ``--``, its targets.
    targets : tuple of str
        The targets the call acts on (qualified names); the call must answer for each of them. Empty for a call without targets.
    events_dir : Path or None
        The ``.gupkg\\logs`` folder where the call writes its events file.
    elevated : bool
        Whether the CLI will run this call in an elevated child.
    """

    label: str
    args: tuple[str, ...]
    targets: tuple[str, ...] = ()
    events_dir: Path | None = None
    elevated: bool = False


@dataclass(frozen=True)
class FollowUp:
    """More calls to make after the first ones (a cleanup after an update, a re-run of the targets the person approved).

    Attributes
    ----------
    steps : tuple of Step
        The calls to make next.
    replaces : frozenset of str
        Package names whose earlier entries are replaced by the results of these calls.
    """

    steps: tuple[Step, ...]
    replaces: frozenset[str] = frozenset()


@dataclass
class BatchOutcome:
    """What a batch did, combined over all its calls.

    Attributes
    ----------
    entries : list of dict
        One result entry per target (the shape of the CLI's ``results``), including those the UI synthesized for targets that
        were refused, skipped or lost.
    problems : list of str
        Reasons a call cannot be trusted (kept for the report; each also marks its targets failed).
    exit_code : int
        The combined exit code.
    cancelled : bool
        Whether the person asked to stop.
    calls : list of CallResult
        The calls that were made, in order.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK
    cancelled: bool = False
    calls: list[CallResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether everything ran and succeeded."""
        return self.exit_code == EXIT_OK

    def logs(self) -> list[str]:
        """Return the log files the entries name, without repeats."""
        seen: dict[str, None] = {}
        for entry in self.entries:
            if isinstance(entry.get("log"), str):
                seen.setdefault(entry["log"])
        return list(seen)


def unqualified(target: str) -> str:
    """Return the package name of a target such as ``system:firefox`` (a bare name stays as it is)."""
    mode, colon, rest = target.partition(":")
    return rest if colon and rest and mode in ("system", "user", "portable") else target


def synthetic_entry(package: str, operation: str, status: str, code: int, message: str, *, ok: bool = False, mode: str | None = None) -> dict[str, Any]:
    """Return a result entry for a target the UI itself decided (``refused``, ``skipped``, ``failed`` with no result)."""
    return {
        "package": unqualified(package), "operation": operation, "ok": ok, "status": status, "version": None, "previous": None, "mode": mode,
        "exit_code": code, "receipt": None, "log": None, "warnings": [],
        "error": {"code": status, "message": message},
    }


def exit_code_of(entries: Sequence[dict[str, Any]], problems: Sequence[str] = ()) -> int:
    """Return the combined exit code: zero when every entry succeeded, otherwise the highest code of a failure.

    A skipped entry (stopped after a cancel) counts as 5, a refused one as 3 (the policy would not run it), and any problem that
    makes a result untrustworthy as at least 3.
    """
    codes: list[int] = [EXIT_FAILED] if problems else []
    for entry in entries:
        status = entry.get("status")
        if entry.get("ok") is not True:
            code = entry.get("exit_code")
            codes.append(code if type(code) is int and code else EXIT_FAILED)
        elif status in ("skipped", "cancelled"):
            codes.append(EXIT_INPUT)
        elif status == "refused":
            codes.append(EXIT_FAILED)
    return max(codes, default=EXIT_OK)


class BatchSession:
    """Run the steps of a batch and combine their results.

    Parameters
    ----------
    client : Client
        Runs the ``gupkg`` calls.
    sink : Sink
        Receives the output while the calls run.
    temp_dir : Path, optional
        The only folder where a stop marker may be created (the ``TEMP`` of the client's environment by default).
    """

    def __init__(self, client: Client, sink: Sink, *, temp_dir: Path | None = None) -> None:
        self._client = client
        self._sink = sink
        self._temp_dir = temp_dir or client.temp_dir
        self._stop: StopRequest | None = None
        self.cancelled = False
        self.running = False

    def request_cancel(self) -> str:
        """Stop after the package that is running: ask the running call, and start no further one; return what was done in words."""
        self.cancelled = True
        self._sink.status("Cancelling after the current package…")
        return self._stop.request() if self._stop is not None else "No call is running; the remaining calls will not start."

    async def run(
        self,
        steps: Sequence[Step],
        *,
        extra_entries: Sequence[dict[str, Any]] = (),
        closing: Sequence[str] = (),
        follow_up: Callable[[BatchOutcome], Awaitable[FollowUp | None]] | None = None,
    ) -> BatchOutcome:
        """Run *steps* in order and write the verdict and summary at the end of the log.

        Parameters
        ----------
        steps : sequence of Step
            The calls, in the order they must run.
        extra_entries : sequence of dict
            Entries for targets decided before any call (refused by the policy, failed in the plan); they are part of the outcome.
        closing : sequence of str
            Extra lines for the summary, such as where the logs are.
        follow_up : callable, optional
            Called with the outcome so far after the steps (and again after each follow-up) until it returns ``None``; the steps it
            returns run in the same log, before the verdict. It is never called after a cancel request.

        Returns
        -------
        BatchOutcome
            The combined result; ``exit_code`` is zero only when nothing failed, was refused or was skipped.
        """
        outcome = BatchOutcome(entries=list(extra_entries))
        renderer = EventRenderer()
        self.running = True
        try:
            await self._run_steps(steps, renderer, outcome)

            # Follow-ups (at most three rounds) run before the verdict, so the verdict is still the last thing in the log.
            for _ in range(3):
                more = await follow_up(outcome) if follow_up is not None and not self.cancelled else None
                if more is None:
                    break
                outcome.entries = [entry for entry in outcome.entries if entry.get("package") not in more.replaces]
                await self._run_steps(more.steps, renderer, outcome)
        finally:
            self.running = False
            self._sink.progress(None)

        # The verdict is the last thing in the log: summary lines, extra lines, then the combined exit code.
        outcome.cancelled = outcome.cancelled or self.cancelled
        outcome.exit_code = exit_code_of(outcome.entries, outcome.problems)
        self._sink.lines(renderer.finish())
        summary = summarize_entries(outcome.entries) + [literal(item) for item in closing]
        if outcome.cancelled:
            summary.append("Stopped after the current package at your request.")
        self._sink.lines(verdict_lines(outcome.exit_code, summary))
        self._sink.status("Finished" if outcome.exit_code == EXIT_OK else f"Failed (exit code {outcome.exit_code})")
        return outcome

    async def _run_steps(self, steps: Sequence[Step], renderer: EventRenderer, outcome: BatchOutcome) -> None:
        """Run *steps* in order; after a cancel request no further call starts and its targets are reported as skipped."""
        for step in steps:
            if self.cancelled:
                outcome.cancelled = True
                operation = step.args[0] if step.args else "operation"
                outcome.entries.extend(
                    synthetic_entry(target, operation, "skipped", EXIT_INPUT, "skipped after the cancel request; left unchanged", ok=True)
                    for target in step.targets
                )
                continue
            await self._run_step(step, renderer, outcome)

    async def _run_step(self, step: Step, renderer: EventRenderer, outcome: BatchOutcome) -> None:
        """Run one call, stream its events to the sink and add its trusted entries (or its failures) to *outcome*."""
        self._sink.lines([Text(f"▶ {literal(step.label, limit=200)}" + ("  (administrator rights, one prompt)" if step.elevated else ""), style="bold")])
        self._sink.status(f"Running: {literal(step.label, limit=100)}")
        self._stop = StopRequest(self._temp_dir)

        def show(records: list[dict[str, Any]]) -> None:
            """Hand a batch of records to the display and refresh the live progress."""
            self._sink.lines(renderer.feed(records))
            self._sink.progress(renderer.progress_text())

        result = await self._client.call(step.args, events_dir=step.events_dir, on_records=show, stop=self._stop)
        self._stop = None
        outcome.calls.append(result)
        operation = step.args[0] if step.args else "operation"

        # Anything that makes the result untrustworthy is shown as an error and fails the targets of this call.
        for problem in result.problems:
            outcome.problems.append(problem)
            self._sink.lines([Text(f"Error: {literal(problem)}", style="bold red")])
        for error in result.errors():
            self._sink.lines([Text(f"Error: {literal(error.get('message', error.get('code', 'error')))}", style="bold red")])
            if isinstance(error.get("hint"), str) and error["hint"]:
                self._sink.lines([Text(f"  hint: {literal(error['hint'])}", style="dim")])

        # Entries the call returned are trusted only when the call itself is; the rest become failures for each target.
        entries = [] if result.problems else result.entries()
        answered = {entry.get("package") for entry in entries}
        outcome.entries.extend(entries)
        reason = "; ".join(result.problems) or next((str(error.get("message")) for error in result.errors() if error.get("message")), "") or (
            f"gupkg returned no result for it (exit code {result.exit_code})"
        )
        code = result.exit_code if type(result.exit_code) is int and result.exit_code else EXIT_INTERNAL
        for target in step.targets:
            if unqualified(target) not in answered:
                outcome.entries.append(synthetic_entry(target, operation, "failed", code, reason))
        if not step.targets and not entries and not result.ok:
            outcome.entries.append(synthetic_entry("(no target)", operation, "failed", code, reason))
