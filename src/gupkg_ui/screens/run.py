"""The run view: an append-only log fed from the CLI's events file, with coloured phase headers, one live progress line and the verdict at the end.

A run screen executes a *job* (a batch of ``gupkg`` calls) without ever blocking the interface: the work is an ``asyncio`` task whose ``gupkg``
children write events files that the session follows. Lines go into a ``RichLog`` that only appends (never re-renders what is already there), is
capped at ``MAX_LOG_LINES``, follows the tail until the person scrolls back and resumes when they return to the bottom. The live progress of the
transfer that runs now is a single line under the log; a finished transfer leaves one lasting line in the log. The verdict (``Done`` or
``Failed``) and the summary are the last lines of the log and are repeated in the status line.

While a job runs, Escape (and ``q``) ask "Cancel after the current package?" (decision D37): an answer of yes asks the running CLI to stop after its
current package, which finishes cleanly while the rest is skipped, and no further call is started. The screen cannot be left while a call runs, so no
operation is ever abandoned silently.

Usage and API
-------------
Push ``RunScreen(app, title, job)`` where ``job(session, sink, interaction)`` returns a ``BatchOutcome``; the screen is dismissed with that outcome. The
questions a job asks (confirmations, a missing checksum) appear as dialogs on top of the run view.

Implementation Approach
-----------------------
The ``ScreenSink`` is the only piece that touches widgets, and it is only called on the interface's own loop (the session is an ``await``-based task), so
no widget is ever changed from another thread.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import RichLog, Static

from ..interaction import Interaction, Job
from ..plans import PlannedTarget
from ..render import literal
from ..session import EXIT_INTERNAL, BatchOutcome, BatchSession
from .frame import FrameScreen
from .modals import cancel_dialog, checksum_dialog, ConfirmScreen

# The most lines the log keeps; older lines scroll out of memory (the log files hold everything).
MAX_LOG_LINES = 5000


class RunLog(RichLog):
    """An append-only log that follows its tail until the person scrolls up and resumes at the bottom."""

    def __init__(self) -> None:
        super().__init__(max_lines=MAX_LOG_LINES, wrap=True, highlight=False, markup=False, auto_scroll=False)

    def append(self, lines: Sequence[Text]) -> None:
        """Add *lines* at the end; stay at the tail only if the view was at the tail."""
        follow = self.max_scroll_y - self.scroll_y <= 1
        for line in lines:
            self.write(line, scroll_end=follow)


class ScreenSink:
    """The ``Sink`` of a run screen: log lines, the live progress line and the run state."""

    def __init__(self, screen: "RunScreen") -> None:
        self._screen = screen

    def lines(self, lines: Sequence[Text]) -> None:
        """Append lasting lines to the log."""
        if lines:
            self._screen.query_one(RunLog).append(lines)

    def progress(self, text: str | None) -> None:
        """Show (or clear) the live progress line."""
        bar = self._screen.query_one("#progress", Static)
        bar.update(Text(literal(text, limit=300) if text else "", no_wrap=True, overflow="ellipsis"))

    def status(self, text: str) -> None:
        """Show the run state in the status line."""
        self._screen.set_state(text)


class RunScreen(FrameScreen):
    """Run a job and show its output; leaves only when the job has ended.

    Parameters
    ----------
    app_env : object
        The program's ``UiEnvironment`` (its client runs the calls).
    title : str
        What is being run, for the status line.
    job : callable
        ``job(session, sink, interaction)`` returning the ``BatchOutcome``.
    """

    DEFAULT_CSS = """
    RunScreen RunLog { height: 1fr; padding: 0 1; }
    RunScreen #progress { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS = [
        Binding("escape", "leave", "Back / cancel"),
        Binding("q", "quit_or_cancel", "Quit"),
        Binding("end", "follow", "Follow"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, app_env, title: str, job: Job) -> None:
        super().__init__()
        self._env = app_env
        self._title = literal(title, limit=200)
        self._job = job
        self.outcome: BatchOutcome | None = None
        self.state = "Starting"
        self.session: BatchSession | None = None

    # -- frame ---------------------------------------------------------------------

    def key_hints(self) -> str:
        """Return the keys that work now: they differ while the job runs and after it ended."""
        if self.outcome is None:
            return "Esc cancel after this package  ↑↓ PgUp PgDn scroll  End follow  ? help"
        return "Esc back  ↑↓ PgUp PgDn Home End scroll  q quit  ? help"

    def status_text(self) -> str:
        """Return the program status with this run's title and state first (before the scope), so a long root never pushes the state out of the line."""
        return str(getattr(self.app, "status_line", lambda own="", own_first=False: own)(f"{self._title}: {self.state}", own_first=True))

    def compose_body(self) -> ComposeResult:
        """Yield the log and the live progress line."""
        yield RunLog()
        yield Static("", id="progress", markup=False)

    def set_state(self, text: str) -> None:
        """Show the run state."""
        self.state = literal(text, limit=200)
        self.refresh_frame()

    # -- running -------------------------------------------------------------------

    def on_mount(self) -> None:
        """Focus the log (so the keyboard scrolls it) and start the job."""
        self.query_one(RunLog).focus()
        self.run_worker(self._execute(), name="run", exclusive=True)

    async def _execute(self) -> None:
        """Run the job; whatever it raises becomes a failed outcome shown in the log, never a crashed interface."""
        sink = ScreenSink(self)
        self.session = BatchSession(self._env.client, sink, colour=self._env.settings.colour)
        interaction = Interaction(confirm=self._confirm, ask_checksum=self._ask_checksum, interactive=True)
        try:
            outcome = await self._job(self.session, sink, interaction)
        except Exception as error:  # noqa: BLE001 - the interface must show a failure, not die with it
            sink.lines([Text(f"Error: {literal(error)}", style="bold red"), Text(f"━━ Failed (exit code {EXIT_INTERNAL}) " + "━" * 30, style="bold red")])
            outcome = BatchOutcome(problems=[str(error)], exit_code=EXIT_INTERNAL)
        self.outcome = outcome
        self.app.last_outcome = outcome
        self.set_state("Finished" if outcome.ok else f"Failed (exit code {outcome.exit_code})")
        self.query_one("#progress", Static).update("")
        self.refresh_frame()

    async def _confirm(self, lines: list[Text], question: str) -> bool:
        """Ask once for a whole plan in a dialog."""
        return bool(await self.app.push_screen_wait(ConfirmScreen(question, lines)))

    async def _ask_checksum(self, item: PlannedTarget) -> bool:
        """Ask in a dialog whether to install a package without a checksum once."""
        return bool(await self.app.push_screen_wait(checksum_dialog(item)))

    # -- keys ----------------------------------------------------------------------

    def action_leave(self) -> None:
        """Escape: go back when the job ended; while it runs, ask whether to cancel after the current package."""
        if self.outcome is not None:
            self.dismiss(self.outcome)
        else:
            self.run_worker(self._ask_cancel(), name="cancel-question")

    def action_quit_or_cancel(self) -> None:
        """``q``: quit when the job ended; while it runs, the same question as Escape (a run is never abandoned silently)."""
        if self.outcome is not None:
            self.app.exit(return_code=self.outcome.exit_code)
        else:
            self.run_worker(self._ask_cancel(), name="cancel-question")

    async def _ask_cancel(self) -> None:
        """Ask "Cancel after the current package?" and, on yes, ask the running call to stop."""
        if self.session is None or self.outcome is not None:
            return
        if await self.app.push_screen_wait(cancel_dialog()) is True and self.outcome is None:
            note = self.session.request_cancel()
            self.query_one(RunLog).append([Text(note, style="dim")])

    def action_follow(self) -> None:
        """Jump to the end of the log and follow it again."""
        self.query_one(RunLog).scroll_end(animate=False)

    def action_help(self) -> None:
        """Show the keys of this view."""
        self.app.action_help()
