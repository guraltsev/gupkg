"""The operations a person starts from a package list or a package view, as one shared set of actions.

Each action asks the questions it needs in a dialog (uninstall, purge, repair of system packages), then pushes a run screen whose job is one of the shared
flows of ``gupkg_ui.flows``: every operation is a ``gupkg`` call (decision D44), with ``--yes`` only for the confirmations already given. Check for updates and
update are separate operations (D11, D23). When the run screen closes, the screen that started it is told to reload what it shows.

Usage and API
-------------
Mix ``PackageActions`` into a screen that has ``self.app`` as a ``UiApp`` and implements ``reload()`` (re-read what the screen shows after a run). Call
``start_check_updates(rows)``, ``start_update(rows)``, ``start_repair(rows)``, ``start_uninstall(rows, purge=...)``, ``start_check(rows, fix=...)`` and
``start_switch(row, version)``; each one runs in a worker so the interface stays responsive.

Implementation Approach
-----------------------
The dialogs are awaited in a worker (``push_screen_wait``), and the run itself is a second screen pushed by ``UiApp.start_run``; the screen below stays on
the stack, so its selection, filter and scroll position survive the run.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text

from ..flows import run_check_updates, run_operation, run_update
from ..interaction import Interaction
from ..plans import PackageRow
from ..render import literal
from ..session import BatchOutcome, BatchSession, Sink
from .modals import ConfirmScreen


def _names(rows: Sequence[PackageRow]) -> str:
    """Return the names of *rows* for a title or a question (a long list is shortened)."""
    names = [literal(row.name, limit=60) for row in rows]
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


class PackageActions:
    """The shared actions of the package list and the package view (needs ``run_worker``, ``app`` and ``reload``)."""

    def reload(self) -> None:  # pragma: no cover - overridden
        """Re-read what the screen shows (called after a run)."""

    # -- running -------------------------------------------------------------------

    def _start(self, title: str, job, *, before=None) -> None:
        """Run *job* in a worker: ask *before* first (it may veto), push the run screen, then reload the screen."""
        async def work() -> None:
            """Ask, run and reload."""
            if before is not None and not await before():
                return
            await self.app.start_run(title, job)
            self.reload()

        self.run_worker(work(), name=f"start {title}")

    async def _confirm(self, question: str, lines: Sequence[Text] = ()) -> bool:
        """Ask a yes-or-no question in a dialog."""
        return bool(await self.app.push_screen_wait(ConfirmScreen(question, lines)))

    # -- the operations ------------------------------------------------------------

    def start_check_updates(self, rows: Sequence[PackageRow]) -> None:
        """Check *rows* for updates: new versions are written and populated, nothing is activated."""
        env = self.app.env

        async def job(session: BatchSession, sink: Sink, interaction: Interaction) -> BatchOutcome:
            """Run ``check-updates`` for the rows."""
            return await run_check_updates(env, rows, session=session, interaction=interaction)

        self._start(f"Check for updates: {_names(rows)}", job)

    def start_update(self, rows: Sequence[PackageRow]) -> None:
        """Plan, confirm once and update *rows*."""
        env = self.app.env

        async def job(session: BatchSession, sink: Sink, interaction: Interaction) -> BatchOutcome:
            """Run the planned update."""
            return await run_update(env, rows, session=session, sink=sink, interaction=interaction)

        self._start(f"Update: {_names(rows)}", job)

    def start_repair(self, rows: Sequence[PackageRow]) -> None:
        """Repair *rows* (``gupkg install`` re-applies the integration and rebuilds a missing App)."""
        env = self.app.env

        async def job(session: BatchSession, sink: Sink, interaction: Interaction) -> BatchOutcome:
            """Run ``install`` for the rows."""
            return await run_operation(env, "install", rows, session=session, options=["--yes"])

        async def before() -> bool:
            """Ask once, naming the packages."""
            return await self._confirm(f"Install or repair {_names(rows)}?", [Text("The active version is installed again; shortcuts, shims and PATH entries are re-applied.")])

        self._start(f"Install: {_names(rows)}", job, before=before)

    def start_uninstall(self, rows: Sequence[PackageRow], *, purge: bool = False) -> None:
        """Uninstall *rows*; with *purge* also delete their folders and data (asked more strongly)."""
        env = self.app.env

        async def job(session: BatchSession, sink: Sink, interaction: Interaction) -> BatchOutcome:
            """Run ``uninstall`` for the rows."""
            return await run_operation(env, "uninstall", rows, session=session, options=["--yes", *(["--purge"] if purge else [])])

        async def before() -> bool:
            """Ask once, saying what stays and what is deleted."""
            if purge:
                return await self._confirm(f"Delete {_names(rows)} and ALL its data?", [
                    Text("The package folder, every version, the App and the data folder are deleted. This cannot be undone."),
                ])
            return await self._confirm(f"Uninstall {_names(rows)}?", [
                Text("Shortcuts, shims and PATH entries are removed. The package folder and its versions stay, so it can be installed again."),
            ])

        self._start(f"{'Purge' if purge else 'Uninstall'}: {_names(rows)}", job, before=before)

    def start_check(self, rows: Sequence[PackageRow], *, fix: bool = False) -> None:
        """Run the health check on *rows*; with *fix* it also repairs them (asked first)."""
        env = self.app.env

        async def job(session: BatchSession, sink: Sink, interaction: Interaction) -> BatchOutcome:
            """Run ``check`` for the rows."""
            return await run_operation(env, "check", rows, session=session, options=["--fix", "--yes"] if fix else [], fix=fix)

        async def before() -> bool:
            """Ask once before anything is changed."""
            return await self._confirm(f"Repair {_names(rows)} where the check finds drift?", [Text("Missing integration is re-applied and a missing App is rebuilt.")])

        self._start(f"Check{' and fix' if fix else ''}: {_names(rows)}", job, before=before if fix else None)

    def start_switch(self, row: PackageRow, version: str) -> None:
        """Activate another local *version* of *row* (the way back)."""
        env = self.app.env

        async def job(session: BatchSession, sink: Sink, interaction: Interaction) -> BatchOutcome:
            """Run ``switch`` for the row."""
            return await run_operation(env, "switch", [row], session=session, options=["--yes"], trailing=[version])

        async def before() -> bool:
            """Ask once, naming the version."""
            return await self._confirm(f"Switch {_names([row])} to {literal(version, limit=60)}?", [Text("The version is activated; the integration follows it.")])

        self._start(f"Switch: {_names([row])} to {literal(version, limit=60)}", job, before=before)
