"""The Textual application that both UI programs build on: one app that never exits to change screens, with one shared status line.

``gupkg-manager`` and ``gupkg-tui`` are each a single ``UiApp``. Opening a package, running an operation and going back push and pop screens
of this one application (the screen below keeps its filter, selection and scroll position); nothing restarts. The app keeps what the status line shows
(the run's scope, the program's own counts, the state of the run, the path of the log) and refreshes the line on every screen when it changes. A terminal that is too narrow
or short loses only the tail of a clipped line, never an action.

Usage and API
-------------
Subclass ``UiApp``: override ``status_parts()`` (the program's counts and registry state) and ``help_lines()``, push the first screen from
``on_mount``, and call ``refresh_status()`` after anything the status line shows. ``await self.start_run(title, job)`` pushes a run screen and returns the
outcome when the person leaves it. ``last_outcome`` is the outcome of the latest run, which a program prints to the console after it closes.

Implementation Approach
-----------------------
The command palette is turned off (it would be one more chrome element), and the shared CSS removes every border and padding so the content area stays
frameless, as the style guide requires.
"""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from .interaction import Job
from .render import literal
from .screens.frame import FRAME_CSS
from .screens.modals import HelpScreen
from .screens.run import RunScreen
from .session import BatchOutcome
from .startup import UiEnvironment


class UiApp(App[int]):
    """The shared application: status line, help and running jobs.

    Parameters
    ----------
    env : UiEnvironment
        The client, the settings and the scope of the program; the title and the status line say which scope it manages.
    """

    CSS = FRAME_CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+c", "ctrl_c", "Quit", show=False, priority=True)]

    def __init__(self, env: UiEnvironment) -> None:
        super().__init__()
        self.env = env
        self.sub_title = env.scope_label
        self.last_outcome: BatchOutcome | None = None
        self.log_path: str | None = None

    # -- the status line -----------------------------------------------------------

    def status_parts(self) -> list[str]:
        """Return the program's own pieces of the status line (counts, registry); the subclass overrides this."""
        return []

    def status_line(self, own: str = "", *, own_first: bool = False) -> str:
        """Return the status line: the run's scope, the program's counts and registry, then where the latest log is.

        The scope comes first as ``user · <root>`` or ``system · <root>`` (the root shortened: ``~`` for the home folder, the middle of a long path as ``…``),
        so it is visible at 80 columns; a narrow terminal then clips the tail (the registry state), never the scope. The window title (``sub_title``)
        carries the full root. *own* is what a screen adds about itself (the package view: the package and its status), right after the scope. The run view
        passes ``own_first=True``: the state of the run (``Synchronise the registry: Failed``) then comes before the scope, so a long root never pushes it out of an 80-column line.
        """
        scope = literal(self.env.scope_text, limit=80)
        parts = [own, scope, *self.status_parts()] if own_first else [scope, own, *self.status_parts()]
        if self.last_outcome is not None and self.last_outcome.logs():
            parts.append(f"log: {literal(self.last_outcome.logs()[-1], limit=300)}")
        return " · ".join(part for part in parts if part)

    def refresh_status(self) -> None:
        """Show the current hints and status on every screen of the stack."""
        for screen in self.screen_stack:
            refresh = getattr(screen, "refresh_frame", None)
            if refresh is not None:
                refresh()

    # -- help and running ----------------------------------------------------------

    def help_lines(self) -> list[str]:
        """Return the lines of the help screen; the subclass overrides this."""
        return []

    def action_help(self) -> None:
        """Show the keys of the program."""
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen("Keys", self.help_lines()))

    def action_ctrl_c(self) -> None:
        """Ctrl-C never abandons a call that is running (decision D37): it asks the cancel question, or is ignored while a dialog is open.

        The whole screen stack is looked at, not only the top screen: a dialog (the cancel question itself, a checksum question asked during a run) may be on top
        of the run view. With a call running, Ctrl-C on the run view opens "Cancel after the current package?"; with any dialog on top it does nothing (a
        second Ctrl-C while the cancel question is shown keeps the run going, it does not quit). With nothing running (the browser, a plan not yet confirmed,
        a finished run) it quits.
        """
        busy = [item for item in self.screen_stack if isinstance(item, RunScreen) and item.outcome is None and item.session is not None and item.session.running]
        if not busy:
            self.exit(return_code=1)
        elif self.screen is busy[-1]:
            busy[-1].action_leave()

    async def start_run(self, title: str, job: Job) -> BatchOutcome | None:
        """Push a run screen for *job* and return its outcome when the person leaves it (call from a worker)."""
        outcome = await self.push_screen_wait(RunScreen(self.env, title, job))
        self.refresh_status()
        return outcome
