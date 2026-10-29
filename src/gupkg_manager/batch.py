"""The manager's own batch logic: the inventory of every package, which packages an ``--all`` batch acts on, and the batch commands.

``gupkg-manager`` reads the inventory in-process (the core functions behind ``gupkg list --json``) and selects the packages an ``--all`` batch acts on (``update``: those with a newer known
version; ``check-updates``: those that are installed or local; ``check``: every package of the root). The plan, the confirmation, the checksum policy and the
execution (one call of the CLI, in the run's scope) are the shared flows of ``gupkg_ui.flows``, which the batch functions here start with the selection.

Usage and API
-------------
``await load_inventory(env)`` returns the ``PackageRow`` list (``available=True`` adds the registry's packages that are not installed). ``pick(rows, command)``
selects the rows. ``await update_all``, ``await check_updates_all`` and ``await check_all`` run the batch commands and return a ``BatchOutcome``.

Implementation Approach
-----------------------
Selection is a pure function of the inventory, so the browser, the non-interactive commands and the tests agree on which packages a batch holds.
"""

from __future__ import annotations

from collections.abc import Sequence

from gupkg_ui.flows import run_check_updates, run_operation, run_update
from gupkg_ui.interaction import Interaction
from gupkg_ui.plans import PackageRow
from gupkg_ui.session import BatchOutcome, BatchSession, Sink
from gupkg_ui.startup import UiEnvironment


async def load_inventory(env: UiEnvironment, *, available: bool = False) -> list[PackageRow]:
    """Return the packages of the run's root, plus the registry's not-yet-installed ones when *available* is true.

    The read is in-process: it calls the core functions that ``gupkg list --json`` and ``gupkg list --available --json`` use, in the run's scope; no
    subprocess is started (decision D53).

    Raises
    ------
    DocumentError
        If the listing cannot be read.
    Exception
        Whatever the core raises (an unreadable root, say); the caller shows it.
    """
    return await env.source.inventory(available=available)


def pick(rows: Sequence[PackageRow], command: str) -> list[PackageRow]:
    """Return the rows an ``--all`` batch of *command* acts on.

    ``update`` takes the packages that have a newer known version; ``check-updates`` takes every package that is installed or local (a package
    with no tracker simply reports that); any other command takes every package that is in a root.
    """
    statuses = {"update": {"outdated"}, "check-updates": {"installed", "outdated", "local", "broken"}}.get(command)
    return [row for row in rows if (row.status in statuses if statuses is not None else row.status != "available")]


async def update_all(
    env: UiEnvironment, rows: Sequence[PackageRow], *, session: BatchSession, sink: Sink, interaction: Interaction,
) -> BatchOutcome:
    """Plan, confirm once and run the update of *rows*, ending with a cleanup when ``[cleanup] auto`` is on."""
    return await run_update(env, rows, session=session, sink=sink, interaction=interaction)


async def check_updates_all(
    env: UiEnvironment, rows: Sequence[PackageRow], *, session: BatchSession, interaction: Interaction, options: Sequence[str] = (),
) -> BatchOutcome:
    """Run ``check-updates`` on *rows*: new versions are written and populated, nothing is activated."""
    return await run_check_updates(env, rows, session=session, interaction=interaction, options=options)


async def check_all(env: UiEnvironment, rows: Sequence[PackageRow], *, session: BatchSession, fix: bool = False) -> BatchOutcome:
    """Run the health check on *rows* (``fix`` also repairs them, which the caller confirmed: it passes ``--yes``)."""
    options = ["--fix", "--yes"] if fix else []
    return await run_operation(env, "check", rows, session=session, options=options)
