"""The manager's own batch logic: the inventory of every package, which packages an ``--all`` batch acts on, and the batch commands.

``gupkg-manager`` reads the inventory with ``gupkg list --json`` and selects the packages an ``--all`` batch acts on (``update``: those with a newer known
version; ``check-updates``: those that are installed or local; ``check``: every package of a root). The plan, the confirmation, the checksum policy and the
grouped execution (one non-elevated call and one elevated call for all system targets) are the shared flows of ``gupkg_ui.flows``, which the batch
functions here start with the selection.

Usage and API
-------------
``await load_inventory(env)`` returns the ``PackageRow`` list (``available=True`` adds the registry's packages that are not installed). ``pick(rows, command,
mode=...)`` selects the rows. ``await update_all``, ``await check_updates_all`` and ``await check_all`` run the batch commands and return a ``BatchOutcome``.

Implementation Approach
-----------------------
Selection is a pure function of the inventory, so the browser, the non-interactive commands and the tests agree on which packages a batch holds.
"""

from __future__ import annotations

from collections.abc import Sequence

from gupkg_ui.flows import run_check_updates, run_operation, run_update
from gupkg_ui.interaction import Interaction
from gupkg_ui.plans import PackageRow, read_packages
from gupkg_ui.session import BatchOutcome, BatchSession, Sink
from gupkg_ui.startup import UiEnvironment


async def load_inventory(env: UiEnvironment, *, available: bool = False) -> list[PackageRow]:
    """Return the packages of every root (``gupkg list --json``), plus the registry's not-yet-installed ones when *available* is true.

    Raises
    ------
    ClientError
        If ``gupkg`` cannot be run or does not return a valid document.
    """
    document = await env.client.query(["list"])
    rows = read_packages(document)
    if available:
        known = {(row.mode, row.name) for row in rows}
        extra = read_packages(await env.client.query(["list", "--available"]))
        rows.extend(row for row in extra if (row.mode, row.name) not in known)
    return rows


def pick(rows: Sequence[PackageRow], command: str, *, mode: str | None = None) -> list[PackageRow]:
    """Return the rows an ``--all`` batch of *command* acts on, optionally only those of *mode*.

    ``update`` takes the packages that have a newer known version; ``check-updates`` takes every package that is installed or local (a package
    with no tracker simply reports that); any other command takes every package that is in a root.
    """
    statuses = {"update": {"outdated"}, "check-updates": {"installed", "outdated", "local", "broken"}}.get(command)
    chosen = [row for row in rows if (row.status in statuses if statuses is not None else row.status != "available")]
    return [row for row in chosen if mode is None or row.mode == mode]


async def update_all(
    env: UiEnvironment, rows: Sequence[PackageRow], *, session: BatchSession, sink: Sink, interaction: Interaction,
) -> BatchOutcome:
    """Plan, confirm once and run the update of *rows*, grouped by mode, ending with a cleanup when ``[cleanup] auto`` is on."""
    return await run_update(env, rows, session=session, sink=sink, interaction=interaction)


async def check_updates_all(
    env: UiEnvironment, rows: Sequence[PackageRow], *, session: BatchSession, interaction: Interaction, options: Sequence[str] = (),
) -> BatchOutcome:
    """Run ``check-updates`` on *rows*: new versions are written and populated, nothing is activated."""
    return await run_check_updates(env, rows, session=session, interaction=interaction, options=options)


async def check_all(env: UiEnvironment, rows: Sequence[PackageRow], *, session: BatchSession, fix: bool = False) -> BatchOutcome:
    """Run the health check on *rows* (``fix`` also repairs them, which the caller confirmed: it passes ``--yes``)."""
    options = ["--fix", "--yes"] if fix else []
    return await run_operation(env, "check", rows, session=session, options=options, fix=fix)
