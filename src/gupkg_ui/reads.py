"""Read the inventory, a package and the registry state in-process, with the same core functions that ``gupkg list|info|registry status --json`` use.

The UI programs never start the CLI to read (decision D53): a read is a call of ``gupkg.viewdata`` on a ``Catalog`` of the run's scope, and its typed
result (``ListDocument``, ``InfoRow``, ``RegistryStatusDocument`` of ``gupkg.report.documents``) is the very object the CLI turns into its document. Operations
(install, update ...) still go through ``Client``. A read that fails raises: ``DocumentError`` for a document the reader cannot understand, or the core's own
exception; the caller shows the message on screen and the full text in the run log.

Usage and API
-------------
``Reads(scope, system)`` is the handle (``UiEnvironment.source`` builds it): ``await reads.inventory(available=False)`` returns the ``PackageRow`` list (``available=True`` adds
the registry's packages that are not installed), ``await reads.info(target)`` returns the ``InfoRow`` of one package and ``await reads.registry()`` returns the
``RegistryStatusDocument``. Tests replace the three synchronous primitives ``list_document``, ``info_document`` and ``registry_document``.

Implementation Approach
-----------------------
The primitives are synchronous and run in a worker thread (``asyncio.to_thread``), so reading a large root never blocks the event loop. Merging the available rows
into the inventory is done here, once, so the real reads and the test double share it.
"""

from __future__ import annotations

import asyncio

from gupkg.catalog.catalog import Catalog
from gupkg.model.scope import Scope
from gupkg.report.documents import DocumentError, InfoDocument, InfoRow, ListDocument, RegistryStatusDocument
from gupkg.system.port import System
from gupkg.viewdata import info_document, list_document, registry_status

from .plans import PackageRow


def one_info(document: InfoDocument, target: str) -> InfoRow:
    """Return the package of an ``info`` document for *target*; raise ``DocumentError`` with the CLI's reason when there is none."""
    if document.rows:
        return document.rows[0]
    reasons = "; ".join(str(item.get("message")) for item in document.errors if item.get("message"))
    raise DocumentError(f"gupkg has no information about {target}" + (f": {reasons}" if reasons else ""))


class Reads:
    """The in-process reads of a UI program, in the scope of its run.

    Parameters
    ----------
    scope : Scope
        The run's scope (its mode and root).
    system : System
        The machine boundary used for reading.
    """

    def __init__(self, scope: Scope, system: System) -> None:
        self.scope = scope
        self.system = system

    # -- the primitives (synchronous) ----------------------------------------------

    def list_document(self, available: bool) -> ListDocument:
        """Return what ``gupkg list --json`` (or ``list --available --json``) lists."""
        return list_document(Catalog(self.scope, self.system), self.scope, "available" if available else None)

    def info_document(self, target: str) -> InfoRow:
        """Return what ``gupkg info <target> --json`` knows."""
        return one_info(info_document(Catalog(self.scope, self.system), self.scope, [target]), target)

    def registry_document(self) -> RegistryStatusDocument:
        """Return what ``gupkg registry status --json`` reports."""
        return registry_status(self.scope)

    # -- what the programs call ----------------------------------------------------

    async def inventory(self, *, available: bool = False) -> list[PackageRow]:
        """Return the packages of the run's root, plus the registry's not-yet-installed ones when *available* is true."""
        rows = [PackageRow.of(row) for row in (await asyncio.to_thread(self.list_document, False)).rows]
        if available:
            known = {row.name for row in rows}
            extra = (await asyncio.to_thread(self.list_document, True)).rows
            rows.extend(PackageRow.of(row) for row in extra if row.name not in known)
        return rows

    async def info(self, target: str) -> InfoRow:
        """Return the details of one package (a name or a folder)."""
        return await asyncio.to_thread(self.info_document, target)

    async def registry(self) -> RegistryStatusDocument:
        """Return the state of the configured registries."""
        return await asyncio.to_thread(self.registry_document)
