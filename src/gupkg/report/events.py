"""Define the events a gupkg run reports and the channel that delivers them.

Everything a run wants a person or a log to know is an immutable event:
the run starting, a package starting, a phase, a step, download progress, a
line printed by a child process, a warning, an error, and the closing
summary. Producers call ``emit(event)``; the reporters installed by the front
end (a console renderer, a log file, a UI) receive each event. Nothing in this
module writes to a terminal or a file.

Usage and API
-------------
Producers import ``emit``, ``phase``, ``package_scope`` and
``subprocess_lines``. A front end builds reporters, combines them with
``MultiReporter`` and installs them with ``use_reporter``. ``event_to_dict``
returns the JSON shape of an event, which is the machine contract written to
``.jsonl`` logs and event files (``API_VERSION`` names its version).

Implementation Approach
-----------------------
The active reporter lives in a ``contextvars.ContextVar``. With no reporter
installed ``emit`` does nothing, so library code never prints. A thread
started with ``threading.Thread`` does not inherit the variable (only
``asyncio.to_thread`` and other copied-context paths do): code that emits
from such a thread must capture the reporter first, as ``subprocess_lines``
does for the threads that read a child's pipes. Warnings and
errors are de-duplicated per package, so a problem that is reported live and
again in a result is delivered once. Child-process lines are produced on
reader threads that do not inherit the context variable, so
``subprocess_lines`` captures the reporter on the calling thread.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol, Sequence

# Version of the event JSON contract, reported by ``RunStart``.
API_VERSION = 1


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStart:
    """Announce a run: the command, its arguments, mode, elevation and settings layers."""

    command: str
    argv: tuple[str, ...]
    mode: str
    elevated: bool
    version: str
    settings_layers: tuple[str, ...] = ()
    api: int = API_VERSION


@dataclass(frozen=True)
class PackageStart:
    """Begin the part of a run that concerns one package.

    ``view`` is a plain description of the package; it holds at least ``name``
    and ``path`` (the package folder, which owns the package's logs).
    """

    view: dict[str, Any]


@dataclass(frozen=True)
class PhaseStart:
    """Begin a named phase of the current package's work."""

    phase: str


@dataclass(frozen=True)
class Step:
    """Report one detail line; ``kind`` is ``info``, ``ok``, ``add`` or ``remove``."""

    text: str
    kind: str = "info"


@dataclass(frozen=True)
class Progress:
    """Report transfer progress; ``total`` is ``None`` while the size is unknown.

    A producer sends a last event with ``done == total`` when the transfer ends.
    """

    task: str
    done: int
    total: int | None
    unit: str = "B"


@dataclass(frozen=True)
class SubprocessLine:
    """Carry one output line of a child process (``cmd`` is the program name).

    ``stream`` is always ``"output"`` in this release: ``System.run`` reports
    lines of both pipes through one callback that does not say which pipe a
    line came from.
    """

    cmd: str
    line: str
    stream: str = "output"


@dataclass(frozen=True)
class Prompt:
    """Report a question the run is about to ask, with its default answer if any."""

    text: str
    default: str | None = None


@dataclass(frozen=True)
class Warning:  # noqa: A001 - the name is part of the documented event list
    """Report a problem that did not stop the work."""

    text: str


@dataclass(frozen=True)
class Error:
    """Report a failure, with an optional hint on how to fix it."""

    text: str
    hint: str | None = None


@dataclass(frozen=True)
class PhaseEnd:
    """End a phase: ``status`` is ``ok``, ``failed`` or ``skipped``; ``elapsed`` is in seconds."""

    phase: str
    status: str
    elapsed: float


@dataclass(frozen=True)
class PackageEnd:
    """End the part of a run that concerned one package, with its result record."""

    result: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunEnd:
    """Close the run with the summary text people read last and the exit code."""

    summary: str
    exit_code: int


# Every event type, in the order the design lists them.
EVENT_TYPES = (
    RunStart, PackageStart, PhaseStart, Step, Progress, SubprocessLine,
    Prompt, Warning, Error, PhaseEnd, PackageEnd, RunEnd,
)


def event_to_dict(event: Any) -> dict[str, Any]:
    """Return the JSON-ready record of *event*: ``{"event": <snake_case type>, <fields>}``.

    Tuples become lists. The record is the public machine contract, so field
    names and the event names never change within one ``API_VERSION``.
    """
    # Convert the class name to its stable snake_case wire name.
    name = type(event).__name__
    wire = "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")
    record: dict[str, Any] = {"event": wire}
    for item in dataclasses.fields(event):
        value = getattr(event, item.name)
        record[item.name] = list(value) if isinstance(value, tuple) else value
    return record


# ---------------------------------------------------------------------------
# Reporters and the active reporter
# ---------------------------------------------------------------------------


class Reporter(Protocol):
    """Receive every event of a run; implementations must be safe to call from several threads."""

    def handle(self, event: Any) -> None:
        """Process one event; an exception stops that reporter for the rest of the run."""


class MultiReporter:
    """Deliver each event to several reporters, isolating their failures.

    A reporter that raises is dropped and the others receive one ``Warning``
    naming it, so a broken log file or a closed terminal never fails the
    operation (and the failure is reported once).
    """

    def __init__(self, reporters: Sequence[Reporter]) -> None:
        self._reporters = list(reporters)
        self._lock = threading.RLock()

    def handle(self, event: Any) -> None:
        """Pass *event* to every live reporter, dropping any that fail."""
        with self._lock:
            failed: list[tuple[Reporter, Exception]] = []
            for reporter in list(self._reporters):
                try:
                    reporter.handle(event)
                except Exception as exc:  # noqa: BLE001 - a reporter must never fail the run
                    failed.append((reporter, exc))
            for reporter, exc in failed:
                self._reporters.remove(reporter)
                notice = Warning(f"{type(reporter).__name__} stopped reporting: {exc}")
                for other in list(self._reporters):
                    with contextlib.suppress(Exception):
                        other.handle(notice)


# The reporter that ``emit`` delivers to, per execution context.
_ACTIVE: contextvars.ContextVar[Reporter | None] = contextvars.ContextVar("gupkg_reporter", default=None)
# Problems already delivered, so each warning or error appears once per package.
_SEEN: contextvars.ContextVar[set | None] = contextvars.ContextVar("gupkg_seen_problems", default=None)
# Marks "reported by some package" in the de-duplication set.
_ANY = object()
# The package whose events are being emitted, for scoping that de-duplication.
_PACKAGE: contextvars.ContextVar[str | None] = contextvars.ContextVar("gupkg_package", default=None)


def current_reporter() -> Reporter | None:
    """Return the reporter installed for this context, or ``None``."""
    return _ACTIVE.get()


@contextlib.contextmanager
def use_reporter(reporter: Reporter | None) -> Iterator[None]:
    """Install *reporter* for the duration of the block, with fresh de-duplication state."""
    token = _ACTIVE.set(reporter)
    seen_token = _SEEN.set(set())
    package_token = _PACKAGE.set(None)
    try:
        yield
    finally:
        _PACKAGE.reset(package_token)
        _SEEN.reset(seen_token)
        _ACTIVE.reset(token)


def emit(event: Any) -> None:
    """Deliver *event* to the active reporter; do nothing when none is installed.

    A ``Warning`` or ``Error`` with the same text as one already delivered is
    dropped, so each problem is reported once: inside a package scope, a repeat
    within that package; outside any package scope (a run-level summary of
    results), a repeat of anything delivered anywhere in the run.
    """
    reporter = _ACTIVE.get()
    if reporter is None:
        return

    # Drop a repeated problem. Two packages may report the same words, so a
    # package-scoped problem is keyed by its package; a run-level one is also
    # checked against every package's problems (the summary repeats them).
    if isinstance(event, (Warning, Error)):
        seen = _SEEN.get()
        package = _PACKAGE.get()
        kind = type(event).__name__
        if seen is not None:
            if (package, kind, event.text) in seen or (package is None and (_ANY, kind, event.text) in seen):
                return
            seen.add((package, kind, event.text))
            seen.add((_ANY, kind, event.text))
    reporter.handle(event)


def emit_problems(warnings: Sequence[str], errors: Sequence[str], hint: str | None = None) -> None:
    """Emit a ``Warning`` per warning and an ``Error`` per error, skipping any already reported."""
    for text in warnings:
        emit(Warning(text))
    for text in errors:
        emit(Error(text, hint))


# ---------------------------------------------------------------------------
# Producer helpers
# ---------------------------------------------------------------------------


class PhaseHandle:
    """Let the body of a ``phase`` block set the status it ends with."""

    def __init__(self) -> None:
        self.status = "ok"

    def fail(self) -> None:
        """Mark the phase failed without raising."""
        self.status = "failed"

    def skip(self) -> None:
        """Mark the phase skipped."""
        self.status = "skipped"

    def settle(self, result: Any) -> Any:
        """Mark the phase failed when *result* (an object with ``ok``) failed, and return it unchanged."""
        if not getattr(result, "ok", True):
            self.status = "failed"
        return result


@contextlib.contextmanager
def phase(name: str) -> Iterator[PhaseHandle]:
    """Report a phase: ``PhaseStart``, then ``PhaseEnd`` with the elapsed time.

    The phase ends ``failed`` when the block raises (the exception continues),
    otherwise with the status the block set, ``ok`` by default.
    """
    handle = PhaseHandle()
    started = time.monotonic()
    emit(PhaseStart(name))
    try:
        yield handle
    except BaseException:
        emit(PhaseEnd(name, "failed", time.monotonic() - started))
        raise
    emit(PhaseEnd(name, handle.status, time.monotonic() - started))


class PackageHandle:
    """Let the body of a ``package_scope`` block record the package's result."""

    def __init__(self) -> None:
        self.result: dict[str, Any] = {}


@contextlib.contextmanager
def package_scope(name: str, path: Any, **extra: Any) -> Iterator[PackageHandle]:
    """Report the part of a run that concerns one package.

    ``path`` is the package folder (it owns the package's log directory).
    ``root`` (extra) is the root that holds the package, whose ``.gupkg\\logs``
    receives the run log, and ``sole=True`` says the run acts on this one
    package only. The handle's ``result`` goes into ``PackageEnd``; a block
    that raises ends the package with ``ok = False``.
    """
    handle = PackageHandle()
    package_token = _PACKAGE.set(str(path))
    try:
        emit(PackageStart({"name": name, "path": str(path), **extra}))
        try:
            yield handle
        except BaseException:
            handle.result.setdefault("ok", False)
            raise
        finally:
            emit(PackageEnd({"name": name, **handle.result}))
    finally:
        _PACKAGE.reset(package_token)


# Escape sequences a child process may print: CSI (also the single-character
# U+009B form); OSC and the device-control, SOS, PM and APC strings, up to
# BEL, ST, or the end of their line when unterminated (an OSC 8 hyperlink
# loses its URL and keeps its text); and any other two-character escape. The
# string alternative reads its payload with disjoint alternatives and always
# ends in one of three terminators, so it never backtracks: time is linear in
# the line length however many unterminated openers a line holds.
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b[\]PX^_](?:[^\x07\x1b\n]|\x1b(?!\\))*(?:\x07|\x1b\\|(?=\n)|\Z)"
    r"|\x1b."
)
# Control characters other than tab and newline (C0, DEL and the C1 range), and
# the bidirectional overrides and isolates that can make text read in another
# order: they would let output steer a terminal or disguise a line.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def clean_line(text: str) -> str:
    """Return *text* without escape sequences, carriage returns, control characters or bidirectional overrides."""
    return _CONTROL.sub("", _ANSI.sub("", text.replace("\r", "")))


def subprocess_lines(argv: Sequence[Any]) -> Callable[[str], None]:
    """Return an ``on_line`` callback that reports a child process's output as events.

    The reporter is captured now, on the calling thread, because the threads
    that read the child's pipes do not inherit the context. Carriage returns
    and control characters are removed from each line.
    """
    reporter = _ACTIVE.get()
    program = os.path.splitext(os.path.basename(str(argv[0])))[0] if argv else "process"

    def on_line(line: str) -> None:
        """Report one output line."""
        if reporter is not None:
            reporter.handle(SubprocessLine(program, clean_line(line)))

    return on_line
