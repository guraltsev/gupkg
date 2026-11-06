"""The ``gupkg-tui`` application: the package view of one package, with its operations on keys.

``gupkg-tui PATH|NAME`` opens the package view as its first screen; running an operation pushes the run view on top of it and going back pops it, all inside one
application. Escape at the package view leaves the program.

Usage and API
-------------
``TuiApp(env, target).run()`` opens the view; ``last_outcome`` holds the latest run's outcome for the summary printed after the program closes.

Implementation Approach
-----------------------
The program is the shared ``UiApp`` with one root screen; every operation is a ``gupkg`` call through the shared flows.
"""

from __future__ import annotations

from gupkg_ui.app import UiApp
from gupkg_ui.screens.package import PackageScreen
from gupkg_ui.startup import UiEnvironment

# Help text shown by the ``?`` key, one line per entry.
HELP = [
    "Package view",
    "  c checks the package for updates (writes and populates new versions, activates nothing); u updates it (one plan, one confirmation).",
    "  r installs or repairs it, s switches to another local version, x uninstalls it (X also deletes its folder and data).",
    "  h runs the health check (H also repairs), l shows the log, g reloads.",
    "While something runs",
    "  Esc asks whether to cancel after the current package; the package that runs finishes cleanly and the rest is skipped.",
    "Everywhere: ? help, Esc back or quit, q quit.",
]


class TuiApp(UiApp):
    """The single-package application of ``gupkg-tui``.

    Parameters
    ----------
    env : UiEnvironment
        The client, the settings and the scope of the program.
    target : str
        The package name or folder to show.

    Methods
    -------
    on_mount
        Open the package view.
    help_lines
        Return the help text.
    """

    TITLE = "gupkg-tui"

    def __init__(self, env: UiEnvironment, target: str) -> None:
        super().__init__(env)
        self._target = target

    def on_mount(self) -> None:
        """Open the package view."""
        self.push_screen(PackageScreen(self._target, root=True))

    def help_lines(self) -> list[str]:
        """Return the help text.

        Returns
        -------
        list of str
            The lines of the help screen.
        """
        return HELP
