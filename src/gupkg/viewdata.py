"""Turn a ``PackageView`` into the plain data that ``list``, ``info`` and ``--json`` show.

The data is the public machine shape of a package (decision D44, ``"api": 2``): the manager and other tools read
the same keys that the human output prints. Details add what only the disk knows: the versions in the package root
with their problems and labels, the tracker, the link, the last ``check-updates`` and how the active version was
verified. A stored ``verified`` is reported as the install's own report (``self_reported``) in user
mode, because a user-mode receipt can be edited by the user.

Usage and API
-------------
``list_row(view)`` and ``info_row(view)`` return the typed rows of ``gupkg.report.documents``; ``package_data(view, details=False)`` returns one
package as plain data and ``info_data(view)`` is ``package_data`` with every detail. ``list_document(catalog, scope, chosen)``,
``info_document(catalog, scope, targets)`` and ``registry_status(scope)`` return the typed documents that ``gupkg list``, ``info`` and
``registry status`` write with ``--json``; the CLI and the UI programs (which read in-process) both call them. They only read.

Implementation Approach
-----------------------
The summary comes from the view alone (no disk access), and each detail reads one small file through the same readers
the engine uses (receipts, the last track state), so what ``info`` shows is what ``check`` and ``update`` act on. The documents are built
from the dataclasses that the reader (``gupkg.report.documents``) returns, so writer and reader cannot drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .catalog.catalog import Catalog, ListFilter
from .catalog.registry import cache_state, load_registry, registry_cache_dir
from .engine.errors import describe
from .engine.health import install_notes
from .engine.receipts import list_receipts
from .engine.stateio import read_track_state
from .model.manifest import INSTALLER_LABEL
from .model.plan import EXIT_OK
from .model.scope import Scope
from .model.state import PackageView, known_candidates
from .report.documents import (
    InfoDocument, InfoRow, LinkRow, ListDocument, ListRow, NoteRow, RegistryRow, RegistryStatusDocument, TrackerRow, VersionRow,
)
from .report.logfile import list_run_logs


def list_row(view: PackageView) -> ListRow:
    """Return the typed row of one package: name, status, active and newest known version (nothing is checked over the network)."""
    candidates = known_candidates(view)
    return ListRow(
        view.name, view.status, view.installed.version if view.installed is not None else None, candidates[0].version if candidates else None,
        str(view.path) if view.path is not None else None, view.following, tuple(view.warnings), view.restriction, view.restriction_reason,
        tuple(view.broken_reasons),
    )


def info_row(view: PackageView) -> InfoRow:
    """Return the typed details of one package: the row, the versions, tracker, link, last check, receipts and verification notes (reads small files)."""
    local = view.local
    versions = tuple(
        VersionRow(
            state.name, state.status, tuple(state.problems),
            INSTALLER_LABEL if state.manifest is not None and state.manifest.source.installer is not None else None,
        )
        for state in (local.versions if local is not None else ())
    )
    tracker = None if local is None or local.tracker is None else TrackerRow(local.tracker.valid, tuple(local.tracker.problems))
    link = None if view.link is None else LinkRow(view.link.registry, view.link.name, view.link.unlinked)

    # How the active version was verified, and the install's own log.
    notes, receipt = install_notes(view)
    checksum = receipt.get("checksum") if receipt is not None else None
    logs = list_run_logs(view.path) if view.path is not None else []
    return InfoRow(
        list_row(view), versions, tuple(item.name for item in (view.registry.versions if view.registry is not None else ())), tracker, link,
        view.note, tuple(view.diverged), tuple(NoteRow(item.kind, item.message) for item in notes),
        dict(checksum) if isinstance(checksum, dict) else None, any(item.kind == "self-reported" for item in notes),
        str(logs[0]) if logs else None, tuple(list_receipts(view.path)) if view.path is not None else (),
        read_track_state(view.path) if view.path is not None else None,
    )


def package_data(view: PackageView, *, details: bool = False) -> dict[str, Any]:
    """Return the data of one package: name, status, active and newest known version, with details when asked.

    Parameters
    ----------
    view : PackageView
        The package as the catalog describes it.
    details : bool, default=False
        Add the versions, tracker, link, last check, receipts and verification notes (reads small files).

    Returns
    -------
    dict
        JSON-ready data (``ListRow.to_data`` or ``InfoRow.to_data``); ``version`` is the active version, ``newest_known`` the newest version
        an update would activate.
    """
    return info_row(view).to_data() if details else list_row(view).to_data()


def info_data(view: PackageView) -> dict[str, Any]:
    """Return the full data of one package, as ``gupkg info`` shows it."""
    return package_data(view, details=True)


# ---------------------------------------------------------------------------
# The reads of list, info and registry status (the CLI and the UI programs call these)
# ---------------------------------------------------------------------------


def list_views(catalog: Catalog, chosen: str | None = None) -> tuple[list[PackageView], tuple[str, ...]]:
    """Return the views ``gupkg list`` shows for the filter word *chosen* (``installed``, ``available``, ``outdated``, ``broken``, ``local`` or ``None``), and the listing's warnings."""
    filters = ListFilter(
        statuses=frozenset({chosen}) if chosen in ("outdated", "broken", "local", "available") else None,
        include_available=chosen == "available",
    )
    listing = catalog.list(filters)
    return [view for view in listing.views if chosen != "installed" or view.installed is not None], tuple(listing.warnings)


def list_document(catalog: Catalog, scope: Scope, chosen: str | None = None) -> ListDocument:
    """Return the typed ``list`` document of the run's scope for the filter word *chosen* (the document ``gupkg list --json`` writes)."""
    views, warnings = list_views(catalog, chosen)
    return ListDocument(str(scope.mode), str(scope.root), tuple(list_row(view) for view in views), warnings)


def info_document(catalog: Catalog, scope: Scope, targets: Sequence[str]) -> InfoDocument:
    """Return the typed ``info`` document for *targets*: the packages found and one error entry per target that could not be described."""
    rows: list[InfoRow] = []
    errors: list[dict[str, Any]] = []
    code = EXIT_OK
    for target in targets:
        try:
            rows.append(info_row(catalog.resolve(target).view))
        except Exception as error:  # noqa: BLE001 - one unknown target must not hide the others
            exit_code, data = describe(error)
            code = max(code, exit_code)
            errors.append({"target": target, **data})
    return InfoDocument(str(scope.mode), str(scope.root), tuple(rows), tuple(errors), code == EXIT_OK, code)


def registry_status(scope: Scope) -> RegistryStatusDocument:
    """Return the typed state of every registry configured for the run's scope (the document ``gupkg registry status --json`` writes)."""
    rows = []
    for registry in scope.settings.registries:
        cache = registry_cache_dir(scope.root, registry.name)
        state = cache_state(cache, scope)
        snapshot = load_registry(registry.name, cache, scope)
        rows.append(RegistryRow(
            registry.name, registry.source, state.revision, state.synchronized_at, state.last_failure, str(cache), len(snapshot.entries), tuple(snapshot.problems),
        ))
    return RegistryStatusDocument(str(scope.mode), tuple(rows))
