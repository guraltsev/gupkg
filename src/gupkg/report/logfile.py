"""Write a run's events to log files in the package and root log directories.

Every run that acts on a package leaves, in that package's
``<package>\\.gupkg\\logs\\``, a plain-text ``.log`` and a ``.jsonl`` file of the
events. The run as a whole leaves a log in ``<root>\\.gupkg\\logs\\`` that holds
only global information and links to the package logs. A bundled gupkg logs a
run that involves no package into its own package's ``.gupkg\\logs\\``. Both
files are flushed after every line, ``latest.log`` is a copy of the newest
text log, and old runs are pruned when a run starts. gupkg never writes logs
under AppData or ProgramData.

Usage and API
-------------
Build ``RunLog(...)`` with the run's facts and install it next to the console
reporter. Call ``bind(base)`` once the root (or package) the run acts on is
known; events reported earlier are held and written then. Read ``summary_lines()``
for the log paths to print in the closing summary and call ``close()`` at the
end. ``validate_log_file_name`` checks the name given to ``--events-file``;
``running_gupkg_logs_base`` finds where a package-less run of the running gupkg
logs.

Implementation Approach
-----------------------
Each log location is a ``_Sink`` that creates its directory only after the real
path is proven to stay inside the owning package or root, writes both files
line by line, and never follows a user-supplied name out of that directory:
names are a plain allow-listed file name, never a path. A sink that fails is
switched off and reported through the ``warn`` callback exactly once, so
logging never fails the operation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .console import PlainFormatter
from .events import (
    PackageEnd, PackageStart, RunEnd, RunStart, event_to_dict,
)


# ---------------------------------------------------------------------------
# Names and locations
# ---------------------------------------------------------------------------


def validate_log_file_name(name: str) -> str:
    """Return *name* if it is a safe plain file name for an events file, otherwise raise ``ValueError``.

    Only ASCII letters, digits and ``._+-`` are allowed, starting with a letter
    or digit. That rules out separators, ``..``, drive letters, alternate data
    streams (``:``), spaces and every wildcard. Windows device names (``CON``,
    ``NUL``, ``COM1`` ... also with an extension), trailing dots and names that
    would collide with the text logs (``*.log``) are refused too.
    """
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}", name):
        raise ValueError(
            f"Invalid events file name {name!r}: use a plain file name of letters, digits and ._+- "
            "(no folders, no path)"
        )
    if name.endswith("."):
        raise ValueError(f"Invalid events file name {name!r}: it must not end with a dot")

    # Windows treats DEVICE.anything as the device, so test the part before the first dot.
    device = name.split(".", 1)[0].lower()
    reserved = {"con", "prn", "aux", "nul", "conin", "conout", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
    if device in reserved:
        raise ValueError(f"Invalid events file name {name!r}: it names a Windows device")
    if name.lower().endswith(".log") or name.lower().startswith("latest."):
        raise ValueError(f"Invalid events file name {name!r}: .log files and latest.* belong to the text logs")

    # A run's own files are named <stamp>-<command>.log/.jsonl; an events file
    # with such a name would open a second handle on one of them.
    if _RUN_FILE.match(name):
        raise ValueError(f"Invalid events file name {name!r}: it looks like a run's log file")
    return name


def running_gupkg_logs_base(package_directory: Path | None = None) -> Path | None:
    """Return the folder whose ``.gupkg\\logs`` receives logs of a run that acts on no package.

    A bundled gupkg (``<root>\\<package>\\<version>\\gupkg\\gupkg\\...``) logs into its own
    package's folder. An installed gupkg (``<root>\\gupkg\\<version>\\App\\gupkg\\...``)
    logs into its root's data directory. Any other layout, such as a source
    checkout, has no place to log, and ``None`` is returned.

    Parameters
    ----------
    package_directory : Path, optional
        Directory of the running ``gupkg`` Python package; defaults to this one.
    """
    package_dir = Path(package_directory or Path(__file__).resolve().parents[1]).resolve()
    tree = package_dir.parent
    version_dir = tree.parent

    # An installed gupkg runs from the package's App folder: root = three levels up.
    if tree.name.lower() == "app" and (version_dir / "pkg.toml").is_file():
        return version_dir.parent.parent

    # A bundled gupkg sits in the version directory's ``gupkg`` folder: its package is two levels up.
    if tree.name.lower() == "gupkg" and (version_dir / "pkg.toml").is_file():
        return version_dir.parent
    return None


def _slug(text: str) -> str:
    """Return *text* reduced to characters that are safe in a file name."""
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", text).strip("._") or "run"


def _utc_stamp(moment: datetime) -> str:
    """Render *moment* as ``yyyymmddThhmmssZ``."""
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_reparse_or_link(path: Path) -> bool:
    """Return whether *path* is a symlink, junction or other reparse point."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(reparse and getattr(info, "st_file_attributes", 0) & reparse)


# ---------------------------------------------------------------------------
# One log location
# ---------------------------------------------------------------------------


class LogUnavailable(OSError):
    """Report that a log location cannot be created or written."""


# Pattern of the files a run leaves (the timestamp sorts chronologically).
_RUN_FILE = re.compile(r"^(\d{8}T\d{6}Z)-.+\.(log|jsonl)$")


def prune_logs(directory: Path, keep_runs: int, keep_days: int, now: datetime) -> None:
    """Delete the oldest runs in *directory* so that at most ``keep_runs - 1`` remain, and any older than ``keep_days``.

    One run is a ``.log`` and its ``.jsonl`` sharing a name. The directory is
    pruned before a run adds its own files, which makes ``keep_runs`` the
    count after the run. Only files that match the run-file name pattern are
    touched, and links are never followed.
    """
    runs: dict[str, list[Path]] = {}
    for item in directory.iterdir():
        match = _RUN_FILE.match(item.name)
        if match and not _is_reparse_or_link(item) and item.is_file():
            runs.setdefault(item.stem, []).append(item)

    # Newest first by timestamp name; keep the allowed number and drop the aged ones.
    ordered = sorted(runs, reverse=True)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=max(keep_days, 0))
    for index, stem in enumerate(ordered):
        stamp = datetime.strptime(stem[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if index >= max(keep_runs - 1, 0) or stamp < cutoff:
            for item in runs[stem]:
                item.unlink(missing_ok=True)


class _Sink:
    """Own one log directory: a text log, a JSONL log and an optional events file."""

    def __init__(
        self, base: Path, stem: str, *, events_name: str | None, keep_runs: int, keep_days: int, now: datetime,
    ) -> None:
        self.formatter = PlainFormatter()
        self.directory = base / ".gupkg" / "logs"
        self._files: list[Any] = []
        self._json_files: list[Any] = []
        self.text: Any = None

        # The real directory must stay inside the owning base, so a planted
        # link can never redirect a write (an elevated one included).
        real_base = Path(os.path.realpath(base))
        real_logs = Path(os.path.realpath(self.directory))
        if not real_logs.is_relative_to(real_base):
            raise LogUnavailable(f"{self.directory} resolves outside {base}")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            prune_logs(self.directory, keep_runs, keep_days, now)
            self._open_pair(stem)
            if events_name is not None:
                self._open_events_file(events_name)
        except OSError as exc:
            self.close()
            raise LogUnavailable(f"{self.directory}: {exc}") from exc

    def _open_pair(self, stem: str) -> None:
        """Create the ``.log`` and ``.jsonl`` files exclusively, adding a counter on a name clash."""
        for attempt in range(1, 1000):
            name = stem if attempt == 1 else f"{stem}-{attempt}"
            text_path, json_path = self.directory / f"{name}.log", self.directory / f"{name}.jsonl"
            try:
                text = open(text_path, "x", encoding="utf-8", newline="\n")
            except FileExistsError:
                continue
            try:
                events = open(json_path, "x", encoding="utf-8", newline="\n")
            except FileExistsError:
                text.close()
                text_path.unlink(missing_ok=True)
                continue
            self.text, self.text_path, self.json_path = text, text_path, json_path
            self._files += [text, events]
            self._json_files.append(events)
            return
        raise OSError("no free log file name")

    def _open_events_file(self, name: str) -> None:
        """Create the caller-named events file, refusing anything that is not a plain file in this directory."""
        validate_log_file_name(name)
        path = self.directory / name
        refusal = OSError(f"{name} would not be a plain file inside {self.directory}")

        # A stale file from an earlier run is replaced; anything else that
        # already has this name (a link, a junction, a directory, a file with a
        # second hard link that another folder shares) is refused, never opened.
        if os.path.lexists(path):
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise refusal
            path.unlink()

        # Create it exclusively, so that a link planted after the check above
        # makes the creation fail instead of being followed.
        handle = open(path, "x", encoding="utf-8", newline="\n")
        self._files.append(handle)
        self._json_files.append(handle)

        # Prove what was created is a plain file with a single name (nobody
        # linked it to another place in the meantime).
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise refusal

    def write_line(self, text: str) -> None:
        """Append one text line and flush."""
        self.text.write(text + "\n")
        self.text.flush()

    def write_record(self, record: dict[str, Any]) -> None:
        """Append one JSON line to every JSON file and flush."""
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        for handle in self._json_files:
            handle.write(line)
            handle.flush()

    def close(self) -> None:
        """Close every open file."""
        for handle in self._files:
            try:
                handle.close()
            except OSError:
                pass
        self._files.clear()
        self._json_files.clear()

    def publish_latest(self) -> None:
        """Replace ``latest.log`` with a copy of this run's text log."""
        # Copy into a uniquely named file that this call creates exclusively
        # (a planted file or link under a fixed name is never written), then
        # swap it in; replacing a name never follows a link at that name.
        temporary = self.directory / f"latest.{uuid.uuid4().hex}.tmp"
        try:
            with open(self.text_path, "rb") as source, open(temporary, "xb") as target:
                shutil.copyfileobj(source, target)
            os.replace(temporary, self.directory / "latest.log")
        finally:
            temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The reporter
# ---------------------------------------------------------------------------


class RunLog:
    """Reporter that writes a run's events to the run log and to each package's log.

    Parameters
    ----------
    command : str
        Command name used in the file names.
    warn : callable
        Called with one message when a log location fails; the run continues without it.
    events_name : str, optional
        Validated plain file name that also receives the JSON events in every log directory.
    keep_runs, keep_days : int
        Retention (settings ``[logs]``): runs kept per directory and the age limit in days.
    clock : callable, optional
        Returns the current UTC time; tests inject a fixed one.
    """

    def __init__(
        self,
        command: str,
        warn: Callable[[str], None],
        *,
        events_name: str | None = None,
        keep_runs: int = 50,
        keep_days: int = 90,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._command = command
        self._warn = warn
        self._events_name = validate_log_file_name(events_name) if events_name is not None else None
        self._keep_runs, self._keep_days = keep_runs, keep_days
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._started = self._clock()
        self._lock = threading.RLock()
        self._run_start: RunStart | None = None
        self._run_sink: _Sink | None = None
        self._run_failed = False
        self._pending: list[tuple[str, Any, datetime]] = []
        self._packages: dict[str, _Sink | None] = {}
        self._package_names: dict[str, str] = {}
        self._current: _Sink | None = None
        self._in_package = False
        self._closed = False

    # -- binding ---------------------------------------------------------------

    def bind(self, base: Path, package_label: str | None = None) -> None:
        """Open the run log below *base* (a root, or a package for package-less bundled runs).

        Events reported before this call are written now. Calling it again has no effect.
        """
        with self._lock:
            if self._run_sink is not None or self._run_failed or self._closed:
                return
            stem = f"{_utc_stamp(self._started)}-{_slug(self._command)}" + (f"-{_slug(package_label)}" if package_label else "")
            try:
                self._run_sink = self._open_sink(Path(base), stem, record_start=False)
            except LogUnavailable as exc:
                self._run_failed = True
                self._pending.clear()
                self._warn(f"Cannot write the run log: {exc}")
                return
            queued, self._pending = self._pending, []
            for kind, payload, moment in queued:
                self._write(self._run_sink, kind, payload, moment)

    def _open_sink(self, base: Path, stem: str, *, record_start: bool) -> _Sink:
        """Create a log location below *base* and write its header.

        A package log is a stand-alone file, so it also receives the run's
        ``run_start`` record; the run log receives that event from the stream.
        """
        sink = _Sink(
            base, stem, events_name=self._events_name,
            keep_runs=self._keep_runs, keep_days=self._keep_days, now=self._started,
        )
        start = self._run_start
        if start is not None:
            sink.write_line(f"# gupkg {start.version}")
            sink.write_line(f"# command: {start.command}")
            sink.write_line(f"# argv: {' '.join(start.argv)}")
            sink.write_line(f"# mode: {start.mode}")
            sink.write_line(f"# elevated: {'yes' if start.elevated else 'no'}")
            sink.write_line(f"# settings layers: {', '.join(start.settings_layers) or '(none)'}")
            sink.write_line(f"# started: {self._started.astimezone(timezone.utc).isoformat()}")
            if record_start:
                sink.write_record(self._record(start, self._started))
        return sink

    # -- reporter ----------------------------------------------------------------

    def handle(self, event: Any) -> None:
        """Route one event to the log location it belongs to."""
        with self._lock:
            if self._closed:
                return
            moment = self._clock()
            if isinstance(event, RunStart):
                self._run_start = event
            if isinstance(event, PackageStart):
                self._begin_package(event, moment)
                return
            if isinstance(event, PackageEnd):
                sink = self._current
                if sink is not None:
                    self._write(sink, "event", event, moment)
                self._current = None
                self._in_package = False
                self._to_run("event", event, moment)
                return
            if isinstance(event, RunEnd):
                self._end_run(event, moment)
                return

            # Package output stays in the package's log; everything else is global.
            if self._in_package:
                if self._current is not None:
                    self._write(self._current, "event", event, moment)
            else:
                self._to_run("event", event, moment)

    def _begin_package(self, event: PackageStart, moment: datetime) -> None:
        """Switch to the package's log, creating it (and linking it from the run log) on first use."""
        path = str(event.view.get("path", ""))
        name = str(event.view.get("name", path))
        self._in_package = True

        # The first package names the root the run acts on, unless the front
        # end already bound it; a run on a single package carries its name.
        package_root = event.view.get("root")
        if self._run_sink is None and package_root:
            self.bind(Path(str(package_root)), name if event.view.get("sole") else None)
        if path not in self._packages:
            try:
                sink = self._open_sink(
                    Path(path), f"{_utc_stamp(self._started)}-{_slug(self._command)}", record_start=True
                )
            except LogUnavailable as exc:
                self._packages[path] = None
                self._warn(f"Cannot write the log of package {name}: {exc}")
            else:
                self._packages[path] = sink
                self._package_names[path] = name
                self._to_run("link", {
                    "package": name, "log": str(sink.text_path), "events": str(sink.json_path),
                    "events_file": str(sink.directory / self._events_name) if self._events_name else None,
                }, moment)
        self._current = self._packages[path]
        self._to_run("event", event, moment)
        if self._current is not None:
            self._write(self._current, "event", event, moment)

    def _end_run(self, event: RunEnd, moment: datetime) -> None:
        """Write the footers, publish ``latest.log`` everywhere and close the files."""
        self._to_run("event", event, moment)
        sinks = [s for s in [self._run_sink, *self._packages.values()] if s is not None]
        for sink in sinks:
            try:
                for line in sink.formatter.finish():
                    sink.write_line(line)
                sink.write_line(f"# exit code: {event.exit_code}")
                sink.write_line(f"# finished: {moment.astimezone(timezone.utc).isoformat()}")
                sink.publish_latest()
            except OSError as exc:
                self._warn(f"Cannot finish the log {sink.text_path}: {exc}")
        self.close()

    def close(self) -> None:
        """Close every log file; later events are ignored."""
        with self._lock:
            self._closed = True
            for sink in [self._run_sink, *self._packages.values()]:
                if sink is not None:
                    sink.close()

    # -- writing -------------------------------------------------------------------

    def _to_run(self, kind: str, payload: Any, moment: datetime) -> None:
        """Write to the run log, or hold the item until the run log is bound."""
        if self._run_sink is not None:
            self._write(self._run_sink, kind, payload, moment)
        elif not self._run_failed:
            self._pending.append((kind, payload, moment))

    def _write(self, sink: _Sink, kind: str, payload: Any, moment: datetime) -> None:
        """Write one event or link to *sink*; a failing sink is switched off with one warning."""
        try:
            if kind == "link":
                sink.write_line(f"Package {payload['package']}: {payload['log']}")
                sink.write_record({"event": "log_link", **payload, "ts": moment.astimezone(timezone.utc).isoformat()})
            else:
                for line in sink.formatter.feed(payload):
                    sink.write_line(line)
                sink.write_record(self._record(payload, moment))
        except Exception as exc:  # noqa: BLE001 - logging must never fail the operation
            self._disable(sink, exc)

    def _disable(self, sink: _Sink, exc: Exception) -> None:
        """Switch a failed sink off and report it once."""
        sink.close()
        if sink is self._run_sink:
            self._run_sink, self._run_failed = None, True
        for key, value in list(self._packages.items()):
            if value is sink:
                self._packages[key] = None
        if self._current is sink:
            self._current = None
        self._warn(f"Cannot write the log {sink.text_path}: {exc}")

    @staticmethod
    def _record(event: Any, moment: datetime) -> dict[str, Any]:
        """Return the JSON record of *event* with its UTC timestamp."""
        record = event_to_dict(event)
        record["ts"] = moment.astimezone(timezone.utc).isoformat()
        return record

    # -- results -------------------------------------------------------------------

    def summary_lines(self) -> list[str]:
        """Return ``Log: <path>`` lines for the run log and every package log written."""
        with self._lock:
            lines = []
            if self._run_sink is not None:
                lines.append(f"Run log: {self._run_sink.text_path}")
            for path, sink in self._packages.items():
                if sink is not None:
                    lines.append(f"Log of {self._package_names[path]}: {sink.text_path}")
            return lines

    def paths(self) -> list[Path]:
        """Return the text logs written, run log first."""
        with self._lock:
            sinks = [self._run_sink, *self._packages.values()]
            return [s.text_path for s in sinks if s is not None]
