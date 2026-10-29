"""The typed shape of the documents ``gupkg ... --json`` writes (``"api": 2``), and the one reader that turns them back into typed rows.

The CLI builds its ``list``, ``info`` and ``registry status`` documents from the dataclasses of this module (``ListRow``, ``InfoRow``,
``RegistryRow`` and the documents that hold them), and the manager and any other program read the same documents with ``read_list``,
``read_info``, ``read_registry_status``, ``read_plan`` and ``read_results``. Writer and reader share the types, so they cannot drift, and
a program that reads a document never parses it with rules of its own.

Usage and API
-------------
``read_list(document)`` returns a ``ListDocument`` (the run's ``mode`` and ``root``, the ``rows``, or the ``names`` of ``list --names``).
``read_info(document)`` returns an ``InfoDocument`` of ``InfoRow`` items. ``read_registry_status(document)`` returns a
``RegistryStatusDocument``. ``read_plan(document)`` returns a ``PlanDocument`` of ``PlanItem`` entries (a ``PlanData`` when a plan
exists, else the ``ResultEntry`` that says why not). ``read_results(document)`` returns a ``ResultsDocument`` of ``ResultEntry`` items.
Every reader raises ``DocumentError`` with a message that names the field (``packages[2].version: expected text, got int``).
``ListDocument.to_data()``, ``InfoDocument.to_data()`` and ``RegistryStatusDocument.to_data()`` return the JSON-ready document.

Implementation Approach
-----------------------
A reader never trusts a type: text must be ``str``, flags ``bool``, counts ``int`` (a ``bool`` is not a count), a list must be a list of
the right items, and a field the writer always writes must be there (a field that is ``null`` by design is read as ``None``). A document
whose ``ok`` is false and that carries no rows raises ``DocumentError`` with the CLI's own error message, so a failed call is never read
as an empty inventory. Extra keys are ignored: a newer writer may add fields within one ``api``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..model.checksum import INVALID, MISSING, PRESENT, ChecksumPlan
from .events import API_VERSION


class DocumentError(ValueError):
    """Report a ``--json`` document that cannot be read: the message names the field and what was found there."""


# ---------------------------------------------------------------------------
# Field readers
# ---------------------------------------------------------------------------


def _kind(value: Any) -> str:
    """Return the name of the JSON type of *value*, for a message."""
    return "null" if value is None else type(value).__name__


def _here(where: str, key: str) -> str:
    """Return the path of the field *key* inside *where*."""
    return f"{where}.{key}" if where else key


def _object(value: Any, where: str) -> Mapping[str, Any]:
    """Return *value* when it is a JSON object."""
    if not isinstance(value, Mapping):
        raise DocumentError(f"{where or 'the document'}: expected an object, got {_kind(value)}")
    return value


def _value(data: Mapping[str, Any], key: str, where: str) -> Any:
    """Return the field *key* of *data*; a missing field is an error (``null`` is a value)."""
    if key not in data:
        raise DocumentError(f"{where or 'the document'}: the field '{key}' is missing")
    return data[key]


def _text(data: Mapping[str, Any], key: str, where: str, *, null: bool = False) -> str | None:
    """Return the text field *key*; ``None`` only when *null* allows it."""
    value = _value(data, key, where)
    if isinstance(value, str) or (null and value is None):
        return value
    raise DocumentError(f"{_here(where, key)}: expected {'text or null' if null else 'text'}, got {_kind(value)}")


def _flag(data: Mapping[str, Any], key: str, where: str) -> bool:
    """Return the boolean field *key*."""
    value = _value(data, key, where)
    if not isinstance(value, bool):
        raise DocumentError(f"{_here(where, key)}: expected true or false, got {_kind(value)}")
    return value


def _count(data: Mapping[str, Any], key: str, where: str) -> int:
    """Return the integer field *key* (a boolean is not a number)."""
    value = _value(data, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DocumentError(f"{_here(where, key)}: expected a number, got {_kind(value)}")
    return value


def _list(data: Mapping[str, Any], key: str, where: str) -> list[Any]:
    """Return the list field *key*."""
    value = _value(data, key, where)
    if not isinstance(value, list):
        raise DocumentError(f"{_here(where, key)}: expected a list, got {_kind(value)}")
    return value


def _texts(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    """Return the list-of-text field *key*."""
    items = _list(data, key, where)
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise DocumentError(f"{_here(where, key)}[{index}]: expected text, got {_kind(item)}")
    return tuple(items)


def _objects(data: Mapping[str, Any], key: str, where: str) -> tuple[Mapping[str, Any], ...]:
    """Return the list-of-objects field *key*."""
    return tuple(_object(item, f"{_here(where, key)}[{index}]") for index, item in enumerate(_list(data, key, where)))


def _optional_object(data: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any] | None:
    """Return the object field *key*, or ``None`` when it is ``null``."""
    value = _value(data, key, where)
    return None if value is None else _object(value, _here(where, key))


def _head(document: Any, command: str | None = None) -> Mapping[str, Any]:
    """Check the envelope (``api``, ``command``, ``ok``, ``exit_code``) of *document* and return it as an object."""
    data = _object(document, "")
    api = _value(data, "api", "")
    if isinstance(api, bool) or api != API_VERSION:
        raise DocumentError(f"api: expected {API_VERSION}, got {api!r}")
    name = _text(data, "command", "")
    if command is not None and name != command:
        raise DocumentError(f"command: expected '{command}', got '{name}'")
    _flag(data, "ok", "")
    _count(data, "exit_code", "")
    return data


def _failed(data: Mapping[str, Any]) -> DocumentError | None:
    """Return the error for a document that says ``ok: false`` (its first error message), or ``None`` when it succeeded."""
    if data["ok"] is True:
        return None
    errors = data.get("errors")
    messages = [str(item.get("message")) for item in errors if isinstance(item, Mapping) and item.get("message")] if isinstance(errors, list) else []
    return DocumentError(f"gupkg {data['command']} failed (exit code {data['exit_code']}): {'; '.join(messages) or 'no reason given'}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListRow:
    """One package of ``gupkg list --json`` (and the head of an ``info`` row).

    Attributes
    ----------
    name, status : str
        Identity and derived status (``installed``, ``outdated``, ``broken``, ``local``, ``available`` ...).
    version, newest_known, path, following : str or None
        The active version (``None`` for an ``available`` row), the newest version an update would activate, the package folder
        (``None`` before it exists) and the followed registry entry.
    warnings, problems : tuple of str
        What the catalog warns about; why the package is broken.
    restriction, restriction_reason : str or None
        For a registry package the run's mode cannot install, ``system only`` or ``user only`` and the refusal text.
    """

    name: str
    status: str
    version: str | None
    newest_known: str | None
    path: str | None
    following: str | None
    warnings: tuple[str, ...] = ()
    restriction: str | None = None
    restriction_reason: str | None = None
    problems: tuple[str, ...] = ()

    @classmethod
    def from_data(cls, data: Mapping[str, Any], where: str = "") -> ListRow:
        """Read one row; every key the CLI writes must be there."""
        return cls(
            _text(data, "name", where) or "", _text(data, "status", where) or "", _text(data, "version", where, null=True),
            _text(data, "newest_known", where, null=True), _text(data, "path", where, null=True), _text(data, "following", where, null=True),
            _texts(data, "warnings", where), _text(data, "restriction", where, null=True), _text(data, "restriction_reason", where, null=True),
            _texts(data, "problems", where),
        )

    def to_data(self) -> dict[str, Any]:
        """Return the row as the CLI writes it."""
        return {
            "name": self.name, "status": self.status, "version": self.version, "newest_known": self.newest_known, "path": self.path,
            "restriction": self.restriction, "restriction_reason": self.restriction_reason, "following": self.following,
            "problems": list(self.problems), "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ListDocument:
    """The ``list --json`` document: the run's scope once, then the rows (or, for ``--names``, the names).

    Attributes
    ----------
    mode, root : str
        The mode and the root of the run, written once.
    rows : tuple of ListRow
        The packages (empty for a ``--names`` document).
    warnings : tuple of str
        What the listing warns about.
    names : tuple of str, optional
        The names of a ``--names`` document, ``None`` otherwise.
    """

    mode: str
    root: str
    rows: tuple[ListRow, ...] = ()
    warnings: tuple[str, ...] = ()
    names: tuple[str, ...] | None = None

    def to_data(self, command: str = "list") -> dict[str, Any]:
        """Return the document as the CLI writes it."""
        head: dict[str, Any] = {"api": API_VERSION, "command": command, "ok": True, "exit_code": 0, "mode": self.mode, "root": self.root}
        if self.names is not None:
            return {**head, "names": list(self.names)}
        return {**head, "packages": [row.to_data() for row in self.rows], "warnings": list(self.warnings)}


def read_list(document: Any) -> ListDocument:
    """Return the typed ``list`` document (``--available`` and ``--names`` forms included); raise ``DocumentError`` when it cannot be read."""
    data = _head(document, "list")
    if (error := _failed(data)) is not None:
        raise error
    mode, root = _text(data, "mode", ""), _text(data, "root", "")
    if "names" in data and "packages" not in data:
        return ListDocument(mode or "", root or "", names=_texts(data, "names", ""))
    rows = tuple(ListRow.from_data(_object(item, f"packages[{index}]"), f"packages[{index}]") for index, item in enumerate(_list(data, "packages", "")))
    return ListDocument(mode or "", root or "", rows, _texts(data, "warnings", ""))


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionRow:
    """One version directory of a package: its name, status, problems and the label (``runs vendor installer``) when it has one."""

    name: str
    status: str
    problems: tuple[str, ...] = ()
    label: str | None = None


@dataclass(frozen=True)
class TrackerRow:
    """The local tracker: whether it is valid and what is wrong with it."""

    valid: bool
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinkRow:
    """The link to a registry entry: the registry, the entry's name and whether the package was unlinked on purpose."""

    registry: str
    name: str
    unlinked: bool


@dataclass(frozen=True)
class NoteRow:
    """One thing verification or the receipt says (``unverified``, ``self-reported`` ...): its kind and message."""

    kind: str
    message: str


@dataclass(frozen=True)
class InfoRow:
    """Everything ``gupkg info --json`` knows about one package.

    Attributes
    ----------
    row : ListRow
        Identity, status and versions.
    versions : tuple of VersionRow
        Every version directory in the package root.
    registry_versions : tuple of str
        The versions the followed registry entry pins.
    tracker : TrackerRow, optional
        The local tracker, ``None`` when there is none.
    link : LinkRow, optional
        The link to a registry entry, ``None`` when there is none.
    not_followed : str, optional
        Why the package follows no registry entry, when it says so.
    diverged : tuple of str
        The versions whose content differs from the registry's.
    notes : tuple of NoteRow
        How the active version was verified.
    checksum : mapping, optional
        What the receipt recorded about the download's checksum.
    self_reported : bool
        Whether the verification is the install's own report (a user-mode receipt can be edited).
    log : str, optional
        The log of the last run on the package.
    receipts : tuple of str
        The versions that have a receipt.
    last_check : mapping, optional
        The last ``check-updates`` as recorded.
    """

    row: ListRow
    versions: tuple[VersionRow, ...] = ()
    registry_versions: tuple[str, ...] = ()
    tracker: TrackerRow | None = None
    link: LinkRow | None = None
    not_followed: str | None = None
    diverged: tuple[str, ...] = ()
    notes: tuple[NoteRow, ...] = ()
    checksum: Mapping[str, Any] | None = None
    self_reported: bool = False
    log: str | None = None
    receipts: tuple[str, ...] = ()
    last_check: Mapping[str, Any] | None = None

    @classmethod
    def from_data(cls, data: Mapping[str, Any], where: str = "") -> InfoRow:
        """Read one ``info`` package."""
        tracker = _optional_object(data, "tracker", where)
        link = _optional_object(data, "link", where)
        versions = tuple(
            VersionRow(
                _text(item, "name", at) or "", _text(item, "status", at) or "", _texts(item, "problems", at), _text(item, "label", at, null=True),
            )
            for index, item in enumerate(_objects(data, "versions", where)) for at in (f"{_here(where, 'versions')}[{index}]",)
        )
        notes = tuple(
            NoteRow(_text(item, "kind", at) or "", _text(item, "message", at) or "")
            for index, item in enumerate(_objects(data, "notes", where)) for at in (f"{_here(where, 'notes')}[{index}]",)
        )
        at_tracker, at_link = _here(where, "tracker"), _here(where, "link")
        return cls(
            ListRow.from_data(data, where), versions, _texts(data, "registry_versions", where),
            None if tracker is None else TrackerRow(_flag(tracker, "valid", at_tracker), _texts(tracker, "problems", at_tracker)),
            None if link is None else LinkRow(_text(link, "registry", at_link) or "", _text(link, "name", at_link) or "", _flag(link, "unlinked", at_link)),
            _text(data, "not_followed", where, null=True), _texts(data, "diverged", where), notes, _optional_object(data, "checksum", where),
            _flag(data, "self_reported", where), _text(data, "log", where, null=True), _texts(data, "receipts", where),
            _optional_object(data, "last_check", where),
        )

    def to_data(self) -> dict[str, Any]:
        """Return the package as ``gupkg info --json`` writes it: the list row, then the details."""
        data = self.row.to_data()
        data["versions"] = [
            {"name": item.name, "status": item.status, "problems": list(item.problems), "label": item.label} for item in self.versions
        ]
        data["registry_versions"] = list(self.registry_versions)
        data["tracker"] = None if self.tracker is None else {"valid": self.tracker.valid, "problems": list(self.tracker.problems)}
        data["link"] = None if self.link is None else {"registry": self.link.registry, "name": self.link.name, "unlinked": self.link.unlinked}
        data["not_followed"] = self.not_followed
        data["diverged"] = list(self.diverged)
        data["notes"] = [{"kind": item.kind, "message": item.message} for item in self.notes]
        data["checksum"] = None if self.checksum is None else dict(self.checksum)
        data["self_reported"] = self.self_reported
        data["log"] = self.log
        data["receipts"] = list(self.receipts)
        data["last_check"] = None if self.last_check is None else dict(self.last_check)
        return data


@dataclass(frozen=True)
class InfoDocument:
    """The ``info --json`` document: the run's scope, the packages found and one error per target that could not be read.

    Attributes
    ----------
    mode, root : str
        The mode and the root of the run.
    rows : tuple of InfoRow
        The packages.
    errors : tuple of mapping
        The CLI's error entries (``target``, ``code``, ``message``) of the targets it could not describe.
    ok, exit_code : bool, int
        What the document says about the call.
    """

    mode: str
    root: str
    rows: tuple[InfoRow, ...] = ()
    errors: tuple[Mapping[str, Any], ...] = ()
    ok: bool = True
    exit_code: int = 0

    def to_data(self) -> dict[str, Any]:
        """Return the document as the CLI writes it."""
        return {
            "api": API_VERSION, "command": "info", "ok": self.ok, "exit_code": self.exit_code, "mode": self.mode, "root": self.root,
            "packages": [row.to_data() for row in self.rows], "errors": [dict(item) for item in self.errors],
        }


def read_info(document: Any) -> InfoDocument:
    """Return the typed ``info`` document; raise ``DocumentError`` when it cannot be read, or when it holds no package and says why."""
    data = _head(document, "info")
    rows = tuple(InfoRow.from_data(_object(item, f"packages[{index}]"), f"packages[{index}]") for index, item in enumerate(_list(data, "packages", "")))
    errors = _objects(data, "errors", "")
    if not rows and (error := _failed(data)) is not None:
        raise error
    return InfoDocument(_text(data, "mode", "") or "", _text(data, "root", "") or "", rows, errors, data["ok"], data["exit_code"])


# ---------------------------------------------------------------------------
# registry status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryRow:
    """One configured registry as ``gupkg registry status`` reports it.

    Attributes
    ----------
    name, source : str
        The configured name and source.
    revision, synchronized_at, last_failure : str or None
        The revision that is cached (``None``: never downloaded), when it was synchronised and the last failure.
    cache : str
        The cache folder.
    packages : int
        How many packages the cached revision holds.
    problems : tuple of str
        The entries the cache could not read.
    """

    name: str
    source: str
    revision: str | None
    synchronized_at: str | None
    last_failure: str | None
    cache: str
    packages: int
    problems: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """Return the row as the CLI writes it."""
        return {
            "name": self.name, "source": self.source, "revision": self.revision, "synchronized_at": self.synchronized_at,
            "last_failure": self.last_failure, "cache": self.cache, "packages": self.packages, "problems": list(self.problems),
        }


@dataclass(frozen=True)
class RegistryStatusDocument:
    """The ``registry status --json`` document: the run's mode and one row per configured registry."""

    mode: str
    registries: tuple[RegistryRow, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """Return the document as the CLI writes it."""
        return {
            "api": API_VERSION, "command": "registry status", "ok": True, "exit_code": 0, "mode": self.mode,
            "registries": [row.to_data() for row in self.registries],
        }


def read_registry_status(document: Any) -> RegistryStatusDocument:
    """Return the typed ``registry status`` document; raise ``DocumentError`` when it cannot be read."""
    data = _head(document, "registry status")
    if (error := _failed(data)) is not None:
        raise error
    rows = []
    for index, item in enumerate(_objects(data, "registries", "")):
        at = f"registries[{index}]"
        rows.append(RegistryRow(
            _text(item, "name", at) or "", _text(item, "source", at) or "", _text(item, "revision", at, null=True),
            _text(item, "synchronized_at", at, null=True), _text(item, "last_failure", at, null=True), _text(item, "cache", at) or "",
            _count(item, "packages", at), _texts(item, "problems", at),
        ))
    return RegistryStatusDocument(_text(data, "mode", "") or "", tuple(rows))


# ---------------------------------------------------------------------------
# plan and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorData:
    """The structured error of a result: its stable ``code``, the ``message`` and the optional ``hint``."""

    code: str
    message: str
    hint: str | None = None


@dataclass(frozen=True)
class ResultEntry:
    """The outcome of one operation on one package (one entry of ``results``, or of a plan that cannot be made).

    Attributes
    ----------
    package, operation, status : str
        The package (or the target text), the operation and its status (``updated``, ``up-to-date``, ``failed`` ...).
    ok : bool
        Whether it succeeded.
    version, previous, mode, receipt, log : str or None
        The version now active, the one before, the install mode, the receipt file and the package's log of the run.
    exit_code : int
        0, 2, 3, 4 or 5.
    warnings : tuple of str
        Problems that did not stop the work.
    error : ErrorData, optional
        The structured error of a failed entry.
    details : mapping, optional
        Operation-specific facts.
    """

    package: str
    operation: str
    ok: bool
    status: str
    version: str | None
    previous: str | None
    mode: str | None
    exit_code: int
    receipt: str | None
    log: str | None
    warnings: tuple[str, ...] = ()
    error: ErrorData | None = None
    details: Mapping[str, Any] | None = None

    @classmethod
    def from_data(cls, data: Mapping[str, Any], where: str = "") -> ResultEntry:
        """Read one result entry."""
        error = data.get("error")
        at = _here(where, "error")
        error_data = None
        if error is not None:
            fields = _object(error, at)
            error_data = ErrorData(_text(fields, "code", at) or "", _text(fields, "message", at) or "", _text(fields, "hint", at, null=True) if "hint" in fields else None)
        details = data.get("details")
        return cls(
            _text(data, "package", where) or "", _text(data, "operation", where) or "", _flag(data, "ok", where), _text(data, "status", where) or "",
            _text(data, "version", where, null=True), _text(data, "previous", where, null=True), _text(data, "mode", where, null=True),
            _count(data, "exit_code", where), _text(data, "receipt", where, null=True), _text(data, "log", where, null=True), _texts(data, "warnings", where),
            error_data, None if details is None else _object(details, _here(where, "details")),
        )


@dataclass(frozen=True)
class ResultsDocument:
    """The ``--json`` document of a lifecycle call: one ``ResultEntry`` per target, in order."""

    ok: bool
    exit_code: int
    results: tuple[ResultEntry, ...] = ()
    warnings: tuple[str, ...] = ()


def read_results(document: Any) -> ResultsDocument:
    """Return the typed result document of a lifecycle call; raise ``DocumentError`` when it cannot be read."""
    data = _head(document)
    entries = tuple(ResultEntry.from_data(_object(item, f"results[{index}]"), f"results[{index}]") for index, item in enumerate(_list(data, "results", "")))
    return ResultsDocument(data["ok"], data["exit_code"], entries, _texts(data, "warnings", ""))


@dataclass(frozen=True)
class PlanData:
    """What a plan says it will do for one target.

    Attributes
    ----------
    operation, package, path, mode : str
        The operation, the package, its folder and the install mode.
    version, previous : str or None
        The version the plan activates and the one active now.
    notes : tuple of str
        Facts the plan reports.
    checksum : ChecksumPlan, optional
        The download's checksum status; ``None`` when the plan downloads nothing.
    effects, phases : tuple of mapping
        The integration changes and the phases with their steps, as written.
    """

    operation: str
    package: str
    path: str
    mode: str
    version: str | None
    previous: str | None
    notes: tuple[str, ...] = ()
    checksum: ChecksumPlan | None = None
    effects: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    phases: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)

    @classmethod
    def from_data(cls, data: Mapping[str, Any], where: str = "") -> PlanData:
        """Read the plan of one target; an unreadable checksum marker is an error (it never means ``present``)."""
        populate = data.get("populate")
        checksum = None
        if populate is not None:
            at = _here(where, "populate")
            fields = _object(populate, at)
            raw = fields.get("checksum")
            if raw is not None:
                at = _here(at, "checksum")
                mark = _object(raw, at)
                sha = _text(mark, "sha256", at) or ""
                if sha not in (PRESENT, MISSING, INVALID):
                    raise DocumentError(f"{_here(at, 'sha256')}: '{sha}' is not one of {PRESENT}, {MISSING}, {INVALID}")
                checksum = ChecksumPlan(sha, _text(mark, "url", at) or "", _text(mark, "url_from", at) or "", _flag(mark, "skippable", at))
        return cls(
            _text(data, "operation", where) or "", _text(data, "package", where) or "", _text(data, "path", where) or "", _text(data, "mode", where) or "",
            _text(data, "version", where, null=True), _text(data, "previous", where, null=True), _texts(data, "notes", where), checksum,
            _objects(data, "effects", where), _objects(data, "phases", where),
        )


@dataclass(frozen=True)
class PlanItem:
    """The answer for one target of a ``--plan`` call: a plan, or the result that says why there is none (``up-to-date``, a failure).

    Attributes
    ----------
    target : str
        The target as it was given.
    ok : bool
        Whether a plan exists or nothing needs doing.
    exit_code : int
        The entry's exit code.
    plan : PlanData, optional
        The plan, when one exists.
    result : ResultEntry, optional
        The reason there is no plan.
    """

    target: str
    ok: bool
    exit_code: int
    plan: PlanData | None = None
    result: ResultEntry | None = None


@dataclass(frozen=True)
class PlanDocument:
    """The ``--plan --json`` document: one ``PlanItem`` per target."""

    ok: bool
    exit_code: int
    items: tuple[PlanItem, ...] = ()


def read_plan(document: Any) -> PlanDocument:
    """Return the typed ``--plan --json`` document; raise ``DocumentError`` when it cannot be read."""
    data = _head(document)
    items = []
    for index, entry in enumerate(_objects(data, "plans", "")):
        at = f"plans[{index}]"
        ok = _flag(entry, "ok", at)
        plan = entry.get("plan")
        if ok and plan is not None:
            items.append(PlanItem(_text(entry, "target", at) or "", ok, _count(entry, "exit_code", at), plan=PlanData.from_data(_object(plan, f"{at}.plan"), f"{at}.plan")))
            continue
        result = ResultEntry.from_data(_object(_value(entry, "result", at), f"{at}.result"), f"{at}.result")
        items.append(PlanItem(_text(entry, "target", at) or "", ok, _count(entry, "exit_code", at), result=result))
    return PlanDocument(data["ok"], data["exit_code"], tuple(items))
