"""The package view: one package's versions, link, data folder, last receipt and log, with its operations on keys.

The view answers "what is this package and what can I do with it": its identity and active version first (taken from the directory identity, not from
metadata), then warnings and problems, then the versions in the package root, the registry's versions, the tracker, the link, the data folder and the last receipt and
log. Operations are keys: check for updates (``c``), update (``u``), repair (``r``), switch version (``s``), uninstall (``x``, purge ``X``), health check (``h``),
logs (``l``). Every operation is a ``gupkg`` call through the shared flows; after a run the view reloads. Check and update are separate operations (D11, D23).

Usage and API
-------------
Push ``PackageScreen(target, row=None, root=False)`` in either program. ``target`` is a name or a folder; the view reads ``gupkg info <target> --json``.
With ``root=True`` (``gupkg-tui``) Escape and ``q`` leave the program; otherwise Escape returns to the screen below.

Implementation Approach
-----------------------
The text is built as ``rich.text.Text`` from cleaned fields, so a package name or description with brackets or escape sequences is shown literally. The read happens in a
worker; a failed read is shown as an error line in the view and the operations that need nothing from it (install) stay available.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Static

from ..client import ClientError, targets_after_options
from ..plans import PackageInfo, PackageRow, read_info
from ..render import literal
from .actions import PackageActions
from .frame import FrameScreen
from .logview import LogScreen
from .versions import VersionPicker


def info_lines(info: PackageInfo) -> list[Text]:
    """Return the lines of the package view for *info*: identity first, then problems, versions, registry, tracker, link, data, receipt and log."""
    row = info.row
    installed = literal(row.version) if row.version else "not installed"
    newer = f" → {literal(row.newest_known)}" if row.newest_known and row.newest_known != row.version else ""
    lines = [Text(f"{literal(row.name)} {literal(row.version or '')}  Installed: {installed}", style="bold")]
    lines.append(Text(f"status: {literal(row.status)}{newer}"))
    if row.restriction:
        lines.append(Text(f"{literal(row.restriction)}: {literal(row.restriction_reason or '', limit=400)}", style="yellow"))

    # Problems and warnings first: they change what the person decides.
    for reason in info.problems:
        lines.append(Text(f"Problem: {reason}", style="bold red"))
    for warning in (*row.warnings, *info.diverged):
        lines.append(Text(f"Warning: {warning}", style="yellow"))
    for note in info.notes:
        lines.append(Text(f"Note: {note}", style="dim"))
    if row.path:
        lines.append(Text(f"folder: {literal(row.path)}"))

    # The versions in the package root, with the active one marked in words.
    lines.append(Text(""))
    lines.append(Text("Versions in the package folder", style="bold"))
    for name, status, problems, label in info.versions or (("(none)", "", (), None),):
        mark = "  (active)" if name == row.version else ""
        extra = f"  {label}" if label else ""
        problem = f"  PROBLEM: {'; '.join(problems)}" if problems else ""
        lines.append(Text(f"  {name:<14} {status}{mark}{extra}{problem}"))
    lines.append(Text(f"Registry versions: {', '.join(info.registry_versions) or 'none'}" + (f"   following: {literal(row.following)}" if row.following else "")))
    if info.tracker is None:
        lines.append(Text("Tracker: none"))
    else:
        valid, problems = info.tracker
        lines.append(Text(f"Tracker: {'valid' if valid else 'INVALID: ' + '; '.join(problems)}"))
    if info.link is not None:
        registry, name, unlinked = info.link
        lines.append(Text(f"Link: {'follows no registry entry' if unlinked else f'{literal(registry)}/{literal(name)}'}"))

    # What gupkg last did here.
    data = Path(row.path) / "data" if row.path else None
    lines.append(Text(f"Data folder: {literal(data)}" if data is not None and data.is_dir() else "Data folder: none"))
    lines.append(Text(f"Last receipt: {info.receipts[-1] if info.receipts else 'none'}"))
    if info.last_check:
        lines.append(Text(f"Last check for updates: {info.last_check}"))
    lines.append(Text(f"Log: {literal(info.log) if info.log else 'none yet'}"))
    return lines


class PackageScreen(PackageActions, FrameScreen):
    """Show one package and run operations on it.

    Parameters
    ----------
    target : str
        A package name or a folder.
    row : PackageRow, optional
        The inventory row the view was opened from (shown at once, before the details arrive).
    root : bool, default=False
        Whether this is the program's first screen: Escape then quits the program.
    """

    DEFAULT_CSS = "PackageScreen #info { padding: 0 1; }"
    BINDINGS = [
        Binding("escape", "back", "Back"), Binding("q", "quit", "Quit"), Binding("question_mark", "help", "Help"),
        Binding("c", "check_updates", "Check for updates"), Binding("u", "update", "Update"), Binding("r", "repair", "Repair"),
        Binding("s", "switch", "Switch version"), Binding("x", "uninstall", "Uninstall"), Binding("X", "purge", "Purge"),
        Binding("h", "health", "Health check"), Binding("H", "health_fix", "Check and fix"), Binding("l", "logs", "Logs"),
        Binding("g", "reload", "Reload"),
    ]

    def __init__(self, target: str, row: PackageRow | None = None, *, root: bool = False) -> None:
        super().__init__()
        self._target = target
        self._root = root
        self.info: PackageInfo | None = None
        self._row = row
        self._error: str | None = None

    # -- frame ---------------------------------------------------------------------

    def key_hints(self) -> str:
        """Return the keys of the package view."""
        leave = "Esc quit" if self._root else "Esc back"
        return f"{leave}  c check  u update  r repair  s switch  x remove  l logs  ? help"

    def status_text(self) -> str:
        """Return the package's status and the program's status line."""
        row = self.info.row if self.info is not None else self._row
        base = str(getattr(self.app, "status_line", lambda: "")())
        own = f"{literal(row.name)} {literal(row.status)}" if row is not None else literal(self._target, limit=120)
        return f"{own} · {base}" if base else own

    def compose_body(self) -> ComposeResult:
        """Yield the scrolling text of the view."""
        with VerticalScroll(id="info"):
            yield Static(Text("Reading the package…"), id="text", markup=False)

    # -- loading -------------------------------------------------------------------

    def on_mount(self) -> None:
        """Focus the view and read the package."""
        self.query_one("#info").focus()
        self.reload()

    def reload(self) -> None:
        """Read ``gupkg info`` again (in a worker) and show it."""
        self.run_worker(self._read(), name="read package", exclusive=True, exit_on_error=False)

    async def _read(self) -> None:
        """Run ``gupkg info <target> --json`` and show the package, or the reason it cannot be shown."""
        try:
            info = read_info(await self.app.env.client.query(targets_after_options("info", [], [self._target])))
            self._error = None if info is not None else f"gupkg has no information about {literal(self._target, limit=200)}"
        except ClientError as error:
            info, self._error = None, str(error)
        self.info = info if info is not None else self.info
        if info is not None:
            self._row = replace(info.row, target_text=self._target if Path(self._target).is_absolute() else None)
        lines = info_lines(self.info) if self.info is not None else []
        if self._error:
            lines.insert(0, Text(f"Error: {literal(self._error)}", style="bold red"))

        # The screen may have been left while the read was running; then there is nothing to show it on.
        try:
            self.query_one("#text", Static).update(Text("\n").join(lines) if lines else Text(""))
        except NoMatches:
            return
        self.refresh_frame()

    # -- actions -------------------------------------------------------------------

    def _rows(self) -> list[PackageRow] | None:
        """Return the package as a one-row list for an operation, or ``None`` (after saying so) when it is not known yet."""
        if self._row is None:
            self.notify("The package is not known yet.", severity="warning", markup=False)
            return None
        return [self._row]

    def action_check_updates(self) -> None:
        """Check for updates of this package."""
        if (rows := self._rows()) is not None:
            self.start_check_updates(rows)

    def action_update(self) -> None:
        """Update this package."""
        if (rows := self._rows()) is not None:
            self.start_update(rows)

    def action_repair(self) -> None:
        """Install or repair this package."""
        if (rows := self._rows()) is not None:
            self.start_repair(rows)

    def action_uninstall(self) -> None:
        """Uninstall this package."""
        if (rows := self._rows()) is not None:
            self.start_uninstall(rows)

    def action_purge(self) -> None:
        """Uninstall this package and delete its folder and data."""
        if (rows := self._rows()) is not None:
            self.start_uninstall(rows, purge=True)

    def action_health(self) -> None:
        """Run the health check on this package."""
        if (rows := self._rows()) is not None:
            self.start_check(rows)

    def action_health_fix(self) -> None:
        """Run the health check and repair what it finds."""
        if (rows := self._rows()) is not None:
            self.start_check(rows, fix=True)

    def action_switch(self) -> None:
        """Choose another local version and activate it."""
        if self.info is None or self._row is None:
            self.notify("The package is not known yet.", severity="warning", markup=False)
            return
        others = [name for name, status, _, _ in self.info.versions if name != self.info.row.version]
        if not others:
            self.notify("There is no other version to switch to.", severity="warning", markup=False)
            return

        def chosen(version: str | None) -> None:
            """Switch once a version was picked."""
            if version is not None and self._row is not None:
                self.start_switch(self._row, version)

        self.app.push_screen(VersionPicker(self.info.row.name, others), chosen)

    def action_logs(self) -> None:
        """Show the package's log."""
        if self.info is None:
            return
        row = self.info.row
        self.app.push_screen(LogScreen(self.info.log, Path(row.path) if row.path else None, row.name))

    def action_reload(self) -> None:
        """Read the package again."""
        self.reload()

    def action_help(self) -> None:
        """Show the keys of the program."""
        self.app.action_help()

    def action_back(self) -> None:
        """Escape: return to the screen below, or leave the program when this is its first screen."""
        if self._root:
            self.app.exit(0)
        else:
            self.dismiss(None)

    def action_quit(self) -> None:
        """Leave the program."""
        self.app.exit(0)
