"""Diagnose the health of an installed package (design 9.2, 9.5 and 13; decision D21).

``diagnose`` answers what ``gupkg check`` reports for one package: whether ``current`` is valid and the active ``App`` is
non-empty, whether every effect of the install record is on the machine (a deleted shim, a missing ``PATH`` entry, a changed
shortcut), whether the files of ``App`` still match the fingerprint recorded at install time (decision D41), whether the
version directory still matches the fingerprint that the followed registry publishes (decision D29), and what is
unverified about the install (skipped checksums, an origin that was not verified). It reads and
reports; it never repairs. ``gupkg.engine.lifecycle.check`` repairs with ``--fix`` by running the install repair plan.

Usage and API
-------------
Call ``diagnose(system, view, mode=..., record_manifest=...)``; the ``Diagnosis`` lists problems (what
makes the package unhealthy), notes (what is only worth knowing) and the keys of drifted effects, which the repair plan
applies again.

Implementation Approach
-----------------------
A receipt is a self-report: in user mode the user can edit it, so a stored ``verified`` is shown as a record
and never as proof. What can be checked again is checked again: the files of ``App`` against the recorded hashes and the
version directory against the registry's fingerprint. Nothing is followed through a link and nothing outside the package
is read, except the machine state that the install record names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..model.checksum import unverified_note
from ..model.plan import effect_key
from ..model.state import PackageView
from ..system.port import Mode, System
from .effects import verify_each
from ..services.fingerprint import verify_directory
from .fingerprint import aggregate_digest, app_files, diff_app
from .planner import provided_names
from .receipts import read_receipt, receipt_checksum


@dataclass(frozen=True)
class Problem:
    """One finding: its ``kind`` (``current``, ``app-missing``, ``drift``, ``app-modified``, ``registry-fingerprint``...), text and whether ``--fix`` can repair it."""

    kind: str
    message: str
    fixable: bool = False

    def to_data(self) -> dict[str, object]:
        """Return the JSON-ready finding."""
        return {"kind": self.kind, "message": self.message, "fixable": self.fixable}


@dataclass(frozen=True)
class Diagnosis:
    """The outcome of a health check of one package.

    Attributes
    ----------
    installed : bool
        Whether ``current`` is valid (a package that is not installed has nothing to check).
    problems : tuple of Problem
        What makes the package unhealthy.
    notes : tuple of Problem
        What is only worth knowing: unverified installs, effects that could not be verified.
    drifted : frozenset
        The ``effect_key`` of recorded effects that are missing or changed, for the repair plan.
    """

    installed: bool
    problems: tuple[Problem, ...] = ()
    notes: tuple[Problem, ...] = ()
    drifted: frozenset[tuple[str, ...]] = frozenset()

    @property
    def healthy(self) -> bool:
        """Return whether nothing needs attention."""
        return not self.problems

    @property
    def fixable(self) -> bool:
        """Return whether ``--fix`` can repair at least one problem."""
        return any(item.fixable for item in self.problems)


def diagnose(
    system: System, view: PackageView, *, mode: Mode, record_manifest: bool,
    owners: Mapping[tuple[str, str], str] | None = None,
) -> Diagnosis:
    """Diagnose one package.

    Parameters
    ----------
    system : System
        The machine boundary, used only to read what the install record says gupkg wrote.
    view : PackageView
        The package, built without verifying effects.
    mode : Mode
        The run's mode (a system-mode run does not read the user's environment).
    record_manifest : bool
        The global ``install.record_manifest`` setting; when off, ``App`` files are not compared.
    owners : mapping, optional
        Which other installed package provides each name (``planner.name_owners``); a name the package's record shares with
        another package is reported as a ``name-conflict`` problem. Nothing is changed.

    Returns
    -------
    Diagnosis
        The findings.
    """
    problems: list[Problem] = []
    notes: list[Problem] = []
    if view.local is None:
        return Diagnosis(False)

    # ``current`` and the install record itself.
    current = view.local.current
    if current.status not in ("absent", "valid"):
        problems.append(Problem("current", current.problem or f"current is {current.status}"))
    problems.extend(Problem("record", text) for text in view.problems)
    if view.installed is None:
        return Diagnosis(False, tuple(problems), ())

    # The active App must exist and be non-empty; a missing one is repaired when its source can rebuild it.
    version = view.installed.version
    state = view.local.version(version)
    manifest = state.manifest if state is not None else None
    if not view.installed.app_populated:
        recoverable = bool(manifest is not None and manifest.source.recoverable)
        problems.append(Problem(
            "app-missing", view.installed.app_problem or f"the App of {version} is missing or empty", recoverable,
        ))

    # Every effect of the install record must still be on the machine.
    drifted: set[tuple[str, ...]] = set()
    for effect, drift, unverified in verify_each(system, view.installed.effects, mode=mode):
        if drift:
            drifted.add(effect_key(effect))
            problems.append(Problem("drift", drift, True))
        if unverified:
            notes.append(Problem("unverified-effect", f"not verified: {unverified}"))

    # A name that another installed package of the root also claims (two install records on disk): a problem of both packages.
    for key, item in provided_names(view.installed.effects).items():
        other = (owners or {}).get(key)
        if other is not None:
            problems.append(Problem(
                "name-conflict",
                f"{view.name} and {other} both provide the {item.word} '{item.name}' ({item.where}). Uninstall one of them, "
                "or use a package definition with a different name.",
            ))

    # What the receipt says, and what can be checked again.
    receipt = read_receipt(view.local.path, version)
    if receipt.problem:
        notes.append(Problem("receipt", receipt.problem))
    elif receipt.data is not None:
        notes.extend(_receipt_notes(receipt.data, view))
        if view.installed.app_populated and record_manifest:
            problems.extend(_app_problems(receipt.data, view, state, manifest))
    if state is not None and manifest is not None:
        problems.extend(_registry_problems(view, version, state.path, manifest))
    return Diagnosis(True, tuple(problems), tuple(notes), frozenset(drifted))


def install_notes(view: PackageView) -> tuple[list[Problem], dict | None]:
    """Return the notes about how the active version of *view* was verified, and its receipt data (``None`` without one).

    This is what ``list --details`` and ``info`` show: a skipped checksum, an origin that was not verified, and, in user
    mode, the reminder that a stored ``verified`` is the install's own report and not proof.
    """
    if view.local is None or view.installed is None:
        return [], None
    receipt = read_receipt(view.local.path, view.installed.version)
    if receipt.problem:
        return [Problem("receipt", receipt.problem)], None
    if receipt.data is None:
        return [], None
    return _receipt_notes(receipt.data, view), dict(receipt.data)


def _receipt_notes(data: dict, view: PackageView) -> list[Problem]:
    """Return the notes of a receipt: skipped checksums, an origin that was not verified, a self-reported ``verified``."""
    notes: list[Problem] = []
    record = receipt_checksum(data)
    if "checksum" in data and record is None:
        return [Problem("receipt", "the receipt's checksum record is not valid; the install is treated as unverified")]
    if record is None:
        return notes
    note = unverified_note(record.decision)
    if note is not None:
        notes.append(Problem("unverified", note))
    else:
        origin = data.get("origin")
        if record.decision == "registry-fingerprint" and isinstance(origin, dict) and origin.get("app_covered") is False:
            notes.append(Problem(
                "verified-partly",
                "verified (App not covered): the shipped App is not covered by the registry fingerprint (version 1 of the "
                "fingerprint ignores App); its files are compared only with the install-time record",
            ))
    mode = view.record.mode if view.record is not None else None
    if note is None and mode == "user":
        notes.append(Problem(
            "self-reported",
            f"recorded as {record.decision} in the receipt; in {mode} mode the receipt can be edited by the user, so this is "
            "the install's own report, not proof",
        ))
    return notes


def _app_problems(data: dict, view: PackageView, state, manifest) -> list[Problem]:
    """Compare the files of the active ``App`` with the fingerprint recorded at install time (decision D41)."""
    app_record = data.get("app")
    if not isinstance(app_record, dict) or not app_record.get("recorded"):
        return []
    skip: tuple[str, ...] = ()
    if manifest is not None and manifest.portable is not None:
        skip = manifest.portable.persist
    app = Path(view.installed.path) / "App"
    if not isinstance(app_record.get("files"), dict):
        return _large_app_problems(app_record, app, view, skip)
    try:
        modified, missing, extra = diff_app(app, {str(k): str(v) for k, v in app_record["files"].items()}, skip)
    except OSError as error:
        return [Problem("app-unreadable", f"The App of {view.installed.version} cannot be read: {error}")]
    found = []
    for kind, names, text in (("modified", modified, "changed"), ("missing", missing, "deleted"), ("extra", extra, "added")):
        if names:
            shown = ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
            found.append(Problem(f"app-{kind}", f"files of the App were {text} since the install: {shown}"))
    return found


def _large_app_problems(app_record: dict, app: Path, view: PackageView, skip: Sequence[str]) -> list[Problem]:
    """Compare an ``App`` whose file list was not recorded (too many files) by its one digest; the files cannot be named."""
    fingerprint = app_record.get("fingerprint")
    if not app_record.get("files_omitted") or not isinstance(fingerprint, str):
        return []
    if skip:
        return [Problem("receipt", f"the App of {view.installed.version} has too many files to list and persists data, so it is not compared")]
    try:
        found = aggregate_digest(app_files(app))
    except OSError as error:
        return [Problem("app-unreadable", f"The App of {view.installed.version} cannot be read: {error}")]
    if found == fingerprint.lower():
        return []
    return [Problem("app-modified", f"the App of {view.installed.version} differs from its install fingerprint (too many files to name which)")]


def _registry_problems(view: PackageView, version: str, directory: Path, manifest) -> list[Problem]:
    """Compare the version directory with the fingerprint the followed registry publishes (decision D29)."""
    if view.registry is None:
        return []
    published = view.registry.version(version)
    if published is None or published.fingerprint is None:
        return []
    try:
        problems = verify_directory(directory, published.fingerprint.lower(), published.path)
    except OSError as error:
        return [Problem("registry-fingerprint", f"The fingerprint of {directory} cannot be computed: {error}")]
    if not problems:
        return []
    return [Problem(
        "registry-fingerprint",
        f"{view.name} {version} does not match the fingerprint of registry {view.registry.registry}: " + "; ".join(problems[:21]),
    )]
