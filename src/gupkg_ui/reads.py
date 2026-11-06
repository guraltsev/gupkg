"""Fetch the inventory, a package and the registry state in-process, with the same core functions that ``gupkg list|info|registry status --json`` use.

The UI programs never start the CLI to look anything up (decision D53): each lookup is a call of ``gupkg.viewdata`` on a ``Catalog`` of the run's scope, and its typed
result (``ListDocument``, ``InfoRow``, ``RegistryStatusDocument`` of ``gupkg.report.documents``) is the very object the CLI turns into its document. Operations
(install, update ...) still go through ``Client``. A lookup that fails raises: ``DocumentError`` for a document the reader cannot understand, or the core's own
exception; the caller shows the message on screen and the full text in the run log.

Usage and API
-------------
``Reads(scope, system)`` is the handle (``UiEnvironment.source`` builds it): ``await reads.inventory(available=False)`` returns the ``PackageRow`` list (``available=True`` adds
the registry's packages that are not installed), ``await reads.info(target)`` returns the ``InfoRow`` of one package and ``await reads.registry()`` returns the
``RegistryStatusDocument``. Tests replace the three synchronous primitives ``list_document``, ``info_document`` and ``registry_document``.

Implementation Approach
-----------------------
The primitives are synchronous and run in a background worker (``asyncio.to_thread``), so reading a large root never blocks the event loop. Merging the available rows
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
    """Return the package of an ``info`` document for *target*, or raise ``DocumentError`` with the CLI's reason when there is none.

    Parameters
    ----------
    document : InfoDocument
        The result of ``gupkg.viewdata.info_document`` for one target.
    target : str
        The name or folder that was asked about, used in the message.

    Returns
    -------
    InfoRow
        The first (only) row of the document.

    Raises
    ------
    DocumentError
        If the document has no row; the message carries the CLI's own reasons.
    """
    if document.rows:
        return document.rows[0]

    # No row: collect the reasons the CLI gave, so the person sees why instead of a bare failure.
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

    Methods
    -------
    list_document
        Synchronous primitive that returns the list document.
    info_document
        Synchronous primitive that returns the row of one package.
    registry_document
        Synchronous primitive that returns the registry state.
    inventory
        Return the installed packages, optionally with the available ones.
    info
        Return the details of one package.
    registry
        Return the state of the configured registries.

    Attributes
    ----------
    scope : Scope
        The run's scope.
    system : System
        The machine boundary used for reading.
    """

    def __init__(self, scope: Scope, system: System) -> None:
        self.scope = scope
        self.system = system

    # -- the primitives (synchronous) ----------------------------------------------

    def list_document(self, available: bool) -> ListDocument:
        """Return what ``gupkg list --json`` (or ``list --available --json``) lists.

        Parameters
        ----------
        available : bool
            Whether to list the registry's packages instead of the installed ones.

        Returns
        -------
        ListDocument
            The typed list document.
        """
        return list_document(Catalog(self.scope, self.system), self.scope, "available" if available else None)

    def info_document(self, target: str) -> InfoRow:
        """Return what ``gupkg info <target> --json`` knows.

        Parameters
        ----------
        target : str
            A package name or folder.

        Returns
        -------
        InfoRow
            The details of the package.

        Raises
        ------
        DocumentError
            If the CLI would have no information about *target*.
        """
        return one_info(info_document(Catalog(self.scope, self.system), self.scope, [target]), target)

    def registry_document(self) -> RegistryStatusDocument:
        """Return what ``gupkg registry status --json`` reports.

        Returns
        -------
        RegistryStatusDocument
            The typed registry status.
        """
        return registry_status(self.scope)

    # -- what the programs call ----------------------------------------------------

    async def inventory(self, *, available: bool = False) -> list[PackageRow]:
        """Return the packages of the run's root, plus the registry's not-yet-installed ones when *available* is true.

        Parameters
        ----------
        available : bool
            Whether to add the registry's packages that are not installed.

        Returns
        -------
        list of PackageRow
            The installed rows, then the extra available rows.
        """
        rows = [PackageRow.of(row) for row in (await asyncio.to_thread(self.list_document, False)).rows]

        # Add the registry's rows whose name is not installed, so a package never appears twice.
        if available:
            known = {row.name for row in rows}
            extra = (await asyncio.to_thread(self.list_document, True)).rows
            rows.extend(PackageRow.of(row) for row in extra if row.name not in known)
        return rows

    async def info(self, target: str) -> InfoRow:
        """Return the details of one package.

        Parameters
        ----------
        target : str
            A package name or folder.

        Returns
        -------
        InfoRow
            The details of the package.
        """
        return await asyncio.to_thread(self.info_document, target)

    async def registry(self) -> RegistryStatusDocument:
        """Return the state of the configured registries.

        Returns
        -------
        RegistryStatusDocument
            The typed registry status.
        """
        return await asyncio.to_thread(self.registry_document)
