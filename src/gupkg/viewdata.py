"""Turn a ``PackageView`` into the plain data that ``list``, ``info`` and ``--json`` show.

The data is the public machine shape of a package (decision D44, ``"api": 1``): the manager and other tools read
the same keys that the human output prints. Details add what only the disk knows: the versions in the package root
with their problems and labels, the tracker, the link, the last ``check-updates`` and how the active version was
verified. A stored ``verified`` is reported as the install's own report (``self_reported``) in user
mode, because a user-mode receipt can be edited by the user.

Usage and API
-------------
``package_data(view, details=False)`` returns one package; ``info_data(view)`` is ``package_data`` with every detail.
Both only read.

Implementation Approach
-----------------------
The summary comes from the view alone (no disk access), and each detail reads one small file through the same readers
the engine uses (receipts, the last track state), so what ``info`` shows is what ``check`` and ``update`` act on.
"""

from __future__ import annotations

from typing import Any

from .engine.health import install_notes
from .engine.receipts import list_receipts
from .engine.stateio import read_track_state
from .model.manifest import INSTALLER_LABEL
from .report.logfile import list_run_logs
from .model.state import PackageView, known_candidates


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
        JSON-ready data; ``version`` is the active version, ``newest_known`` the newest version an update would
        activate (nothing is checked over the network).
    """
    candidates = known_candidates(view)
    data: dict[str, Any] = {
        "name": view.name,
        "status": view.status,
        "version": view.installed.version if view.installed is not None else None,
        "newest_known": candidates[0].version if candidates else None,
        "path": str(view.path) if view.path is not None else None,
        "restriction": view.restriction,
        "restriction_reason": view.restriction_reason,
        "following": view.following,
        "problems": list(view.broken_reasons),
        "warnings": list(view.warnings),
    }
    if not details:
        return data

    # The versions in the package root, oldest first, and the ones only the followed registry holds.
    local = view.local
    data["versions"] = [
        {
            "name": state.name, "status": state.status, "problems": list(state.problems),
            "label": INSTALLER_LABEL if state.manifest is not None and state.manifest.source.installer is not None else None,
        }
        for state in (local.versions if local is not None else ())
    ]
    data["registry_versions"] = [item.name for item in (view.registry.versions if view.registry is not None else ())]
    data["tracker"] = None if local is None or local.tracker is None else {
        "valid": local.tracker.valid, "problems": list(local.tracker.problems),
    }
    data["link"] = None if view.link is None else {
        "registry": view.link.registry, "name": view.link.name, "unlinked": view.link.unlinked,
    }
    data["not_followed"] = view.note
    data["diverged"] = list(view.diverged)

    # How the active version was verified, and the install's own log.
    notes, receipt = install_notes(view)
    data["notes"] = [{"kind": item.kind, "message": item.message} for item in notes]
    checksum = receipt.get("checksum") if receipt is not None else None
    data["checksum"] = dict(checksum) if isinstance(checksum, dict) else None
    data["self_reported"] = any(item.kind == "self-reported" for item in notes)
    logs = list_run_logs(view.path) if view.path is not None else []
    data["log"] = str(logs[0]) if logs else None
    data["receipts"] = list_receipts(view.path) if view.path is not None else []
    data["last_check"] = read_track_state(view.path) if view.path is not None else None
    return data


def info_data(view: PackageView) -> dict[str, Any]:
    """Return the full data of one package, as ``gupkg info`` shows it."""
    return package_data(view, details=True)
