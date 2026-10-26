"""The browser: a table of every package with filter, selection and the operations on keys.

The browser lists every package of every root (name, version, mode, status, registry). Up and Down move, Enter opens the package view, ``/`` filters by text,
and the operations are keys: check for updates (``c``, all with ``C``), update (``u``, all outdated with ``U``), install or repair (``i``), uninstall
(``x``), health check (``h``), logs (``l``), toggle the registry's packages that are not installed (``a``), synchronise the registry (``s``), reload (``r``). Opening a package, running an operation
and coming back never restarts the program: the browser stays below, so its filter and selection are kept, and it reloads the inventory after a run.

Usage and API
-------------
``BrowserScreen`` is the first screen of ``ManagerApp``; it reads the inventory with ``gupkg list --json`` through ``load_inventory``.

Implementation Approach
-----------------------
Cells are ``Text`` objects (a ``DataTable`` would otherwise parse a string as markup). Rows are keyed by the qualified target, so the selection can be restored
after a reload, and the filter is applied to the rows already loaded.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Input

from gupkg_ui.client import ClientError, targets_after_options
from gupkg_ui.plans import PackageRow, read_info
from gupkg_ui.render import literal
from gupkg_ui.screens.actions import PackageActions
from gupkg_ui.screens.frame import FrameScreen
from gupkg_ui.screens.logview import LogScreen
from gupkg_ui.screens.package import PackageScreen

from ..batch import load_inventory


class BrowserScreen(PackageActions, FrameScreen):
    """The table of every package."""

    DEFAULT_CSS = """
    BrowserScreen #filter { height: 1; padding: 0 1; display: none; }
    BrowserScreen #filter.on { display: block; }
    BrowserScreen DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("slash", "filter", "Filter"), Binding("escape", "escape", "Back"), Binding("q", "quit", "Quit"), Binding("question_mark", "help", "Help"),
        Binding("c", "check_updates", "Check"), Binding("C", "check_all", "Check all"), Binding("u", "update", "Update"), Binding("U", "update_all", "Update all"),
        Binding("i", "install", "Install"), Binding("x", "uninstall", "Uninstall"), Binding("h", "health", "Health"), Binding("l", "logs", "Logs"),
        Binding("a", "available", "Available"), Binding("s", "sync_registry", "Sync registry"), Binding("r", "reload", "Reload"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[PackageRow] = []
        self.filter_text = ""
        self.available = False
        self.error: str | None = None
        self.loaded = False
        self.hinted = False

    # -- frame ---------------------------------------------------------------------

    def key_hints(self) -> str:
        """Return the keys of the browser."""
        if getattr(self.app, "registry_unsynced", False):
            return "s DOWNLOAD THE REGISTRY  a list available  c check  u update  ? help  q quit"
        return "↑↓ Enter open  / filter  c check  u update  U all  s sync  ? help  q quit"

    def status_text(self) -> str:
        """Return the program's counts and registry state."""
        return str(getattr(self.app, "status_line", lambda: "")())

    def compose_body(self) -> ComposeResult:
        """Yield the filter line (hidden until ``/``) and the table."""
        yield Input(placeholder="filter by name, mode or status (Enter keeps it, Esc clears it)", id="filter", compact=True)
        table = DataTable(cursor_type="row", zebra_stripes=False)
        table.add_columns("Name", "Version", "Mode", "Status", "Registry")
        yield table

    def on_mount(self) -> None:
        """Focus the table and load the inventory."""
        self.query_one(DataTable).focus()
        self.reload()

    # -- the inventory -------------------------------------------------------------

    def reload(self) -> None:
        """Read the inventory again in a worker; the selection and filter are kept."""
        self.run_worker(self._load(), name="inventory", exclusive=True, exit_on_error=False)

    async def _load(self) -> None:
        """Read ``gupkg list --json`` and the registry state, then show the rows."""
        try:
            self.rows = await load_inventory(self.app.env, available=self.available)
            self.error = None
        except ClientError as error:
            self.error = str(error)
        self.loaded = True
        await self.app.read_registry()
        self.app.set_inventory(self.rows, self.error)
        self._fill()
        if self.app.registry_unsynced and not self.hinted:
            self.hinted = True
            self.notify(
                "The registry is not downloaded yet. Press s to download it, then a to list its packages and i to install one.",
                title="Registry", timeout=15, markup=False,
            )

    def _visible(self) -> list[PackageRow]:
        """Return the rows that match the filter text (name, mode, status or registry entry, any case)."""
        needle = self.filter_text.casefold()
        return [row for row in self.rows if not needle or needle in " ".join((row.name, row.mode, row.status, row.following or "")).casefold()]

    def _fill(self) -> None:
        """Rebuild the table from the visible rows and put the cursor back on the row that was selected."""
        table = self.query_one(DataTable)
        keep = self.selected_target()
        table.clear()
        for row in self._visible():
            newer = f" → {row.newest_known}" if row.status == "outdated" and row.newest_known else ""
            table.add_row(
                Text(literal(row.name, limit=60)), Text(literal(row.version or "-", limit=30)), Text(literal(row.mode, limit=12)),
                Text(literal(row.status + newer, limit=60)), Text(literal(row.following or "-", limit=60)), key=row.target,
            )
        if keep is not None and keep in [item.target for item in self._visible()]:
            table.move_cursor(row=table.get_row_index(keep))
        self.refresh_frame()

    def selected_target(self) -> str | None:
        """Return the target of the row under the cursor, or ``None`` when the table is empty."""
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def selected_row(self) -> PackageRow | None:
        """Return the package under the cursor."""
        target = self.selected_target()
        return next((row for row in self.rows if row.target == target), None)

    # -- keys ----------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter opens the package view of the selected package (the browser stays below)."""
        row = self.selected_row()
        if row is not None:
            self.app.push_screen(PackageScreen(row.target, row), lambda _result: self.reload())

    def action_filter(self) -> None:
        """Show the filter line and focus it."""
        box = self.query_one(Input)
        box.add_class("on")
        box.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter as the person types."""
        self.filter_text = event.value
        self._fill()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter keeps the filter and returns to the table."""
        self.query_one(DataTable).focus()

    def action_escape(self) -> None:
        """Escape clears an active filter; with none it leaves the program."""
        box = self.query_one(Input)
        if self.filter_text or box.has_class("on"):
            box.value = ""
            self.filter_text = ""
            box.remove_class("on")
            self.query_one(DataTable).focus()
            self._fill()
        else:
            self.app.exit(0)

    def _one(self) -> list[PackageRow] | None:
        """Return the selected package as a list, or ``None`` when nothing is selected."""
        row = self.selected_row()
        return [row] if row is not None else None

    def action_check_updates(self) -> None:
        """Check the selected package for updates."""
        if (rows := self._one()) is not None:
            self.start_check_updates(rows)

    def action_check_all(self) -> None:
        """Check every listed package for updates."""
        rows = [row for row in self._visible() if row.status in ("installed", "outdated", "local", "broken")]
        if rows:
            self.start_check_updates(rows)

    def action_update(self) -> None:
        """Update the selected package."""
        if (rows := self._one()) is not None:
            self.start_update(rows)

    def action_update_all(self) -> None:
        """Update every outdated package that is listed."""
        rows = [row for row in self._visible() if row.status == "outdated"]
        if rows:
            self.start_update(rows)
        else:
            self.notify("Nothing is outdated.", markup=False)

    def action_install(self) -> None:
        """Install or repair the selected package."""
        if (rows := self._one()) is not None:
            self.start_repair(rows)

    def action_uninstall(self) -> None:
        """Uninstall the selected package."""
        if (rows := self._one()) is not None:
            self.start_uninstall(rows)

    def action_health(self) -> None:
        """Run the health check on the selected package."""
        if (rows := self._one()) is not None:
            self.start_check(rows)

    def action_logs(self) -> None:
        """Show the log of the selected package."""
        row = self.selected_row()
        if row is not None:
            self.run_worker(self._show_log(row), name="logs", exit_on_error=False)

    async def _show_log(self, row: PackageRow) -> None:
        """Find the package's log through ``gupkg info`` and show it."""
        try:
            info = read_info(await self.app.env.client.query(targets_after_options("info", [], [row.target])))
        except ClientError as error:
            self.notify(literal(error), severity="error", markup=False)
            return
        self.app.push_screen(LogScreen(info.log if info else None, Path(row.path) if row.path else None, row.name))

    def action_sync_registry(self) -> None:
        """Download the registries, so that ``a`` has packages to show."""
        self.start_registry_sync()

    def action_available(self) -> None:
        """Show or hide the registry's packages that are not installed."""
        self.available = not self.available
        self.reload()

    def action_reload(self) -> None:
        """Read the inventory again."""
        self.reload()

    def action_help(self) -> None:
        """Show the keys of the program."""
        self.app.action_help()

    def action_quit(self) -> None:
        """Leave the program."""
        self.app.exit(0)
