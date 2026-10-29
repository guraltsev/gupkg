"""The ``gupkg-manager`` application: the browser over the packages of the run's scope, with the scope, the counts and the registry state in the status line.

One application, many screens (design section 15): the browser is the first screen, the package view and the run view are pushed on top of it and popped
back, and the program never exits and restarts to switch. The status line shows the scope (``system · <root>`` or ``user · <root>``, also the window title), the counts (outdated, broken), the registry revision and how long ago it was
synchronised, and the path of the latest log.

Usage and API
-------------
``ManagerApp(env).run()`` opens the browser; ``last_outcome`` holds the outcome of the latest run for the summary printed after the program closes.

Implementation Approach
-----------------------
The registry state is read in-process (the core behind ``gupkg registry status --json``); a failed read puts ``ERROR: ...`` in the status line and its full text in
a notification and in ``problems``, so nothing the program cannot read is dropped silently.
"""

from __future__ import annotations

from datetime import datetime, timezone

from gupkg_ui.app import UiApp
from gupkg_ui.plans import PackageRow
from gupkg_ui.render import literal
from gupkg_ui.startup import UiEnvironment

from .browser import BrowserScreen

HELP = [
    "Browser",
    "  The status line starts with the scope this program manages: system when it runs in an administrator shell, user otherwise.",
    "  Up/Down move, Enter opens the package, / filters, a shows registry packages that are not installed (marked system only or user only when this scope cannot install them), s synchronises the registry (download it first), r reloads.",
    "  c checks the selected package for updates (writes and populates new versions, activates nothing); C checks every listed package.",
    "  u updates the selected package; U updates every outdated package (one plan, one confirmation).",
    "  i installs or repairs, x uninstalls, h runs the health check, l shows the log.",
    "Package view",
    "  c check, u update, r repair, s switch version, x uninstall (X also deletes the data), h health check (H also fixes), l logs, g reload.",
    "While something runs",
    "  Esc asks whether to cancel after the current package; the package that runs finishes cleanly and the rest is skipped.",
    "Everywhere: ? help, q quit, Esc back.",
]


def age(stamp: str | None, now: datetime | None = None) -> str:
    """Return how long ago the ISO time *stamp* was (``2h ago``), ``never`` when there is none and ``?`` when it cannot be read."""
    if not stamp:
        return "never"
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - moment).total_seconds()))
    for limit, unit, size in ((3600, "m", 60), (86400, "h", 3600)):
        if seconds < limit:
            return f"{max(seconds // size, 0)}{unit} ago"
    return f"{seconds // 86400}d ago"


class ManagerApp(UiApp):
    """The browser application of ``gupkg-manager``."""

    TITLE = "gupkg-manager"

    def __init__(self, env: UiEnvironment) -> None:
        super().__init__(env)
        self.rows: list[PackageRow] = []
        self.inventory_error: str | None = None
        self.registry: str = ""
        self.registry_error: str | None = None
        self.registry_unsynced = False
        self.problems: list[str] = []

    def on_mount(self) -> None:
        """Open the browser."""
        self.push_screen(BrowserScreen())

    def help_lines(self) -> list[str]:
        """Return the help text."""
        return HELP

    def set_inventory(self, rows: list[PackageRow], error: str | None) -> None:
        """Remember the loaded inventory for the status line."""
        self.rows, self.inventory_error = rows, error
        self.refresh_status()

    def report_problem(self, what: str, error: BaseException) -> str:
        """Keep the full text of a failed read in ``problems``, show it in a notification and return the one-line form for the status line."""
        full = f"{what}: {type(error).__name__}: {error}"
        self.problems.append(full)
        self.notify(literal(full, limit=2000), title="Read failed", severity="error", timeout=20, markup=False)
        return f"{what}: {literal(str(error), limit=100)}"

    async def read_registry(self) -> None:
        """Read the registry revision and age in-process (what ``gupkg registry status`` reports); a failed read is shown in the status line, never left out silently."""
        self.registry_error = None
        try:
            document = await self.env.source.registry()
        except Exception as error:  # noqa: BLE001 - every failure of the read is shown
            self.registry = ""
            self.registry_error = self.report_problem("registry unreadable", error)
            return
        parts = []
        unsynced = False
        for item in document.registries:
            name = literal(item.name, limit=30)
            unsynced = unsynced or not item.revision
            parts.append(
                f"registry {name} @ {literal(item.revision[:7], limit=12)} (synced {age(item.synchronized_at)})" if item.revision
                else f"registry {name}: NOT DOWNLOADED, press s"
            )
        if not parts:
            parts.append("no registry configured (gupkg registry add NAME SOURCE)")
        self.registry = " · ".join(parts)
        self.registry_unsynced = unsynced

    def status_parts(self) -> list[str]:
        """Return the counts, the registry state and any inventory error."""
        outdated = sum(row.status == "outdated" for row in self.rows)
        broken = sum(row.status == "broken" for row in self.rows)
        available = sum(row.status == "available" for row in self.rows)
        parts = [f"{len(self.rows) - available} packages", f"{outdated} outdated", f"{broken} broken"]
        if available:
            parts.append(f"{available} available in the registry (i installs)")
        if self.inventory_error:
            parts.insert(0, f"ERROR: {literal(self.inventory_error, limit=120)}")
        if self.registry_error:
            parts.insert(1 if self.inventory_error else 0, f"ERROR: {literal(self.registry_error, limit=140)}")
        parts.append(self.registry)
        return parts
