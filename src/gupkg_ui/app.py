"""The Textual application that both UI programs build on: one app that never exits to change screens, with one shared status line.

``gupkg-manager`` and ``gupkg-tui`` are each a single ``UiApp``. Opening a package, running an operation and going back push and pop screens
of this one application (the screen below keeps its filter, selection and scroll position); nothing restarts. The app keeps what the status line shows
(the program's own counts, the state of the run, the path of the log) and refreshes the line on every screen when it changes. A terminal that is too narrow
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
        The client, settings and elevation ability of the program.
    """

    CSS = FRAME_CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+c", "ctrl_c", "Quit", show=False, priority=True)]

    def __init__(self, env: UiEnvironment) -> None:
        super().__init__()
        self.env = env
        self.last_outcome: BatchOutcome | None = None
        self.log_path: str | None = None

    # -- the status line -----------------------------------------------------------

    def status_parts(self) -> list[str]:
        """Return the program's own pieces of the status line (counts, registry); the subclass overrides this."""
        return []

    def status_line(self) -> str:
        """Return the status line: the program's counts and registry, then where the latest log is."""
        parts = [*self.status_parts()]
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
        """Ctrl-C quits the program only when no run is active; a run is cancelled through Escape, never dropped by accident."""
        screen = self.screen
        if isinstance(screen, RunScreen) and screen.outcome is None:
            screen.action_leave()
            return
        self.exit(1)

    async def start_run(self, title: str, job: Job) -> BatchOutcome | None:
        """Push a run screen for *job* and return its outcome when the person leaves it (call from a worker)."""
        outcome = await self.push_screen_wait(RunScreen(self.env, title, job))
        self.refresh_status()
        return outcome
