"""The ``gupkg-manager`` application: the browser over every package, with the counts and the registry state in the status line.

One application, many screens (design section 15): the browser is the first screen, the package view and the run view are pushed on top of it and popped
back, and the program never exits and restarts to switch. The status line shows the counts (outdated, broken), the registry revision and how long ago it was
synchronised, and the path of the latest log.

Usage and API
-------------
``ManagerApp(env).run()`` opens the browser; ``last_outcome`` holds the outcome of the latest run for the summary printed after the program closes.

Implementation Approach
-----------------------
The registry state comes from ``gupkg registry status --json`` (a read-only call); a failure only leaves it out of the status line.
"""

from __future__ import annotations

from datetime import datetime, timezone

from gupkg_ui.app import UiApp
from gupkg_ui.client import ClientError
from gupkg_ui.plans import PackageRow
from gupkg_ui.render import literal
from gupkg_ui.startup import UiEnvironment

from .browser import BrowserScreen

HELP = [
    "Browser",
    "  Up/Down move, Enter opens the package, / filters, a shows registry packages that are not installed, s synchronises the registry (download it first), r reloads.",
    "  c checks the selected package for updates (writes and populates new versions, activates nothing); C checks every listed package.",
    "  u updates the selected package; U updates every outdated package (one plan, one confirmation, one administrator prompt for system packages).",
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

    async def read_registry(self) -> None:
        """Read the registry revision and age (``gupkg registry status``); leave it out of the status line when it cannot be read."""
        try:
            document = await self.env.client.query(["registry", "status"])
        except ClientError:
            self.registry = ""
            return
        parts = []
        for item in document.get("registries", ()):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                revision = item.get("revision") if isinstance(item.get("revision"), str) else None
                name = literal(item["name"], limit=30)
                synced = item.get("synchronized_at") if isinstance(item.get("synchronized_at"), str) else None
                parts.append(f"registry {name} @ {literal(revision[:7], limit=12)} (synced {age(synced)})" if revision else f"registry {name}: not synced")
        self.registry = " · ".join(parts)

    def status_parts(self) -> list[str]:
        """Return the counts, the registry state and any inventory error."""
        outdated = sum(row.status == "outdated" for row in self.rows)
        broken = sum(row.status == "broken" for row in self.rows)
        parts = [f"{len(self.rows)} packages", f"{outdated} outdated", f"{broken} broken"]
        if self.inventory_error:
            parts.insert(0, f"ERROR: {literal(self.inventory_error, limit=120)}")
        parts.append(self.registry)
        return parts
