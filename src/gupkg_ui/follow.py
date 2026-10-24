"""Follow the events files that a ``gupkg --events-file NAME`` call writes, without ever blocking or growing without bound.

A call writes the run's global events to ``<root>\\.gupkg\\logs\\NAME`` and each package's events to
``<package>\\.gupkg\\logs\\NAME``; the global file links to the package files through ``log_link`` records. The file is
data from a process the reader does not control, so everything about it is bounded and strict: a record is at most
``MAX_RECORD_BYTES`` long, a line that is too long or is not a JSON object is reported as one placeholder record and
skipped, a file that is a link or a junction is never opened, a package file must have the exact shape
``<package>\\.gupkg\\logs\\NAME`` and each poll reads a bounded number of bytes.

Usage and API
-------------
Create ``EventStream(global_path)`` for the file the call will write and call ``poll()`` as often as needed: it returns the
new records in the order a person would read them (a package's own records between its ``package_start`` and its
``package_end``), never raises for a missing, unreadable or malformed file, and returns an empty list when nothing is new.
``stream.run_end`` is the ``run_end`` record once it was seen, and ``stream.stop_file`` is the stop marker the run announced
in its latest ``run_start``. ``read_run_end(path)`` reads the last record of a finished file.

Implementation Approach
-----------------------
Each file is read incrementally by byte offset and re-opened for every poll, so no handle stays open on a file that a purge
or a log pruning may delete. A line is split from the bytes only when its newline arrived (a half-written last line is kept
until it is complete, up to the record limit, after which the rest of that line is discarded). The package file of a
package is read between the global ``package_start`` and ``package_end`` records, and the package file's own copies of
``run_start``, ``package_start`` and ``package_end`` are dropped, so the merged stream has each event once.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The longest record (one JSON line) that is parsed; a longer line is skipped and reported by a placeholder record.
MAX_RECORD_BYTES = 1 << 20
# Bytes read from one file in one read, and the number of reads one poll may make (so a huge file never freezes the caller).
CHUNK_BYTES = 1 << 18
MAX_READS_PER_POLL = 8
# Records one poll returns at most; the rest stay in the file for the next poll.
MAX_RECORDS_PER_POLL = 2000

_DUPLICATES_IN_PACKAGE_FILE = frozenset({"run_start", "package_start", "package_end"})


@dataclass
class RecordReader:
    """Read the JSON-object lines that a file gains, from where the last call stopped.

    Parameters
    ----------
    path : Path
        The file to follow. It may not exist yet.

    Attributes
    ----------
    done : bool
        Set when the file proved unusable (a link, a junction or a directory): it is never read again.
    """

    path: Path
    offset: int = 0
    done: bool = False
    _pending: bytes = b""
    _discarding: bool = False
    _skipped: int = 0
    _identity: tuple[int, int] | None = None
    _lines: list[bytes | int] = field(default_factory=list)

    def read(self, limit: int = MAX_RECORDS_PER_POLL) -> list[dict[str, Any]]:
        """Return up to *limit* new records; an empty list when the file is missing, unusable or has nothing new."""
        if self.done:
            return []

        # Refuse anything that is not a plain file before opening it (a link or junction could point anywhere).
        try:
            info = os.lstat(self.path)
        except OSError:
            return []
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or (reparse and getattr(info, "st_file_attributes", 0) & reparse):
            self.done = True
            return [{"event": "unreadable", "reason": f"{self.path.name} is not a plain file"}]

        # A file that shrank, or that is another file under the same name (rotated, replaced), is read again from its start, with one note.
        records: list[dict[str, Any]] = []
        identity = (info.st_dev, info.st_ino) if info.st_ino else None
        if info.st_size < self.offset or (self._identity is not None and identity is not None and identity != self._identity):
            self.offset, self._pending, self._discarding, self._skipped = 0, b"", False, 0
            self._lines.clear()
            records.append({"event": "unreadable", "reason": "the events file was truncated or replaced; it is read again from its start"})
        self._identity = identity

        # Lines that were cut earlier but not yet returned come first; a poll never returns more than *limit* records.
        records.extend(self._parse_lines(limit - len(records)))
        if len(records) >= limit:
            return records

        # Read a bounded amount of new bytes, then cut complete lines from what is pending.
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                for _ in range(MAX_READS_PER_POLL):
                    data = handle.read(CHUNK_BYTES)
                    if not data:
                        break
                    self.offset += len(data)
                    self._split(data)
                    records.extend(self._parse_lines(limit - len(records)))
                    if len(records) >= limit:
                        break
        except OSError:
            return records
        return records

    def _split(self, data: bytes) -> None:
        """Cut *data* into complete lines kept in ``_lines``; a line over the limit is kept as its byte count, one entry per line however long it is."""
        parts = (self._pending + data).split(b"\n")
        self._pending = parts.pop()
        for part in parts:
            if self._discarding:
                # The end of a line whose start was already dropped: its whole length is now known.
                self._lines.append(self._skipped + len(part))
                self._discarding, self._skipped = False, 0
            else:
                self._lines.append(part if len(part) <= MAX_RECORD_BYTES else len(part))

        # No newline yet and already over the limit (or still inside such a line): drop what is held and only count it.
        if self._discarding:
            self._skipped += len(self._pending)
            self._pending = b""
        elif len(self._pending) > MAX_RECORD_BYTES:
            self._discarding, self._skipped, self._pending = True, len(self._pending), b""

    def _parse_lines(self, limit: int) -> list[dict[str, Any]]:
        """Parse up to *limit* of the complete lines into records, putting a placeholder for a line that is not a record."""
        records: list[dict[str, Any]] = []
        taken = 0
        for raw in self._lines:
            if taken >= limit:
                break
            taken += 1
            if isinstance(raw, int):
                records.append({"event": "unreadable", "reason": f"a record of {raw} bytes was longer than the limit of {MAX_RECORD_BYTES} bytes and was skipped"})
                continue
            text = raw.strip()
            if not text:
                continue
            try:
                value = json.loads(text.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                records.append({"event": "unreadable", "reason": "a line is not valid JSON"})
                continue
            if isinstance(value, dict) and isinstance(value.get("event"), str):
                records.append(value)
            else:
                records.append({"event": "unreadable", "reason": "a line is not an event record"})
        del self._lines[:taken]
        return records


class EventStream:
    """Merge a call's global events file with the package files it links to, in reading order.

    Parameters
    ----------
    global_path : Path
        ``<root>\\.gupkg\\logs\\NAME``, the file the call will write (it may not exist yet).
    """

    def __init__(self, global_path: Path) -> None:
        self.global_path = Path(global_path)
        self._name = self.global_path.name
        self._global = RecordReader(self.global_path)
        self._package: RecordReader | None = None
        self._links: dict[str, Path] = {}
        self._held: list[dict[str, Any]] = []
        self.run_end: dict[str, Any] | None = None
        self.stop_file: str | None = None

    def poll(self) -> list[dict[str, Any]]:
        """Return the records that arrived since the last poll, merged in reading order."""
        out: list[dict[str, Any]] = []

        # Inside a package: forward its own records until the global file reports that the package ended.
        while len(out) < MAX_RECORDS_PER_POLL:
            if self._package is not None:
                out.extend(self._take_package_records())
                ended = self._global_until_package_end(out)
                if not ended:
                    break
                continue
            batch = self._next_global()
            if not batch:
                break
            out.extend(batch)
        return out

    # -- the global file ---------------------------------------------------------

    def _next_global(self) -> list[dict[str, Any]]:
        """Return global records up to and including a ``package_start`` (which switches to the package file)."""
        if not self._held:
            self._held = self._global.read()
        records: list[dict[str, Any]] = []
        while self._held:
            record = self._held.pop(0)
            self._note(record)
            records.append(record)
            if record["event"] == "package_start":
                self._enter_package(record)
                break
        return records

    def _global_until_package_end(self, out: list[dict[str, Any]]) -> bool:
        """Forward global records while inside a package; return ``True`` once its ``package_end`` was forwarded."""
        if not self._held:
            self._held = self._global.read()
        while self._held:
            record = self._held.pop(0)
            self._note(record)
            if record["event"] == "package_end":
                # The package's file is complete before the global record: take its last records first.
                out.extend(self._take_package_records())
                self._package = None
                out.append(record)
                return True
            out.append(record)
        return False

    def _note(self, record: dict[str, Any]) -> None:
        """Remember what later steps need from a global record: links, the stop marker and the end of the run."""
        kind = record["event"]
        if kind == "log_link":
            self._remember_link(record)
        elif kind == "run_start" and isinstance(record.get("stop_file"), str):
            self.stop_file = record["stop_file"]
        elif kind == "run_end":
            self.run_end = record

    def _remember_link(self, record: dict[str, Any]) -> None:
        """Accept a package's events file only when it is exactly ``<package>\\.gupkg\\logs\\NAME``."""
        package, text = record.get("package"), record.get("events_file")
        if not isinstance(package, str) or not isinstance(text, str):
            return
        path = Path(text)
        if path.name == self._name and path.parent.name.lower() == "logs" and path.parent.parent.name.lower() == ".gupkg" and path.is_absolute():
            self._links[package] = path

    # -- the package files -------------------------------------------------------

    def _enter_package(self, record: dict[str, Any]) -> None:
        """Start reading the linked file of the package that began, when the global file linked one."""
        view = record.get("view")
        name = view.get("name") if isinstance(view, dict) else None
        link = self._links.get(name) if isinstance(name, str) else None
        self._package = RecordReader(link) if link is not None else None

    def _take_package_records(self) -> list[dict[str, Any]]:
        """Return the package file's new records, without the events that the global file already carries."""
        if self._package is None:
            return []
        return [item for item in self._package.read() if item["event"] not in _DUPLICATES_IN_PACKAGE_FILE]


def read_run_end(path: Path) -> dict[str, Any] | None:
    """Return the last record of the events file *path* when it is a ``run_end``, else ``None`` (missing, unreadable or unfinished)."""
    last: dict[str, Any] | None = None
    reader = RecordReader(Path(path))
    while True:
        batch = reader.read()
        if not batch:
            break
        last = batch[-1]
    return last if last is not None and last["event"] == "run_end" else None
