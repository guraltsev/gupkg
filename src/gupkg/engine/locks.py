"""Keep two gupkg runs from changing one package at the same time (design section 4, ``.gupkg\\locks``).

0.17 had an update lock; without one a second ``install`` or a ``cleanup`` could delete the work folder of a run in
progress. A run that changes a package (install, update, switch, repair, uninstall, cleanup, ``check-updates`` and
``check --fix``) takes ``<package>\\.gupkg\\locks\\package.lock`` first; ``check``, ``list`` and ``info`` read only and take
none.

Usage and API
-------------
``with package_lock(system, package_path, operation, now, elevated=...)`` holds the lock for the block and releases it on
every exit (a failure, a cancel, an exception). ``PackageBusy`` is raised when a live run holds it; its message names the
holder (process, host, operation, start time). ``lock_holder(system, package_path, now)`` returns the live holder of
a package or ``None`` without taking anything (``cleanup`` uses it to leave an in-flight work folder alone).

Implementation Approach
-----------------------
The lock is a small TOML file created exclusively (``O_CREAT | O_EXCL``), so two runs cannot both create it. It holds the
process id, the host name, the operation, the start time and a random token. A lock is *stale* when its process is not
running (judged through ``System.process_is_running``), when it is older than ``MAX_LOCK_AGE`` (a day: no operation takes
that long, and a recycled process id cannot hold a lock for ever), or when its text is unreadable and not brand new. A lock
written on another host is judged by age only. A stale lock is moved aside and the creation is tried again; the moved file
is checked to be the stale one, so a live lock that appeared in between is put back rather than stolen. Release removes
the file only when it still carries this run's token.

An elevated run takes the lock only in an admin-only ``locks`` folder, so a standard user cannot plant a lock file that
keeps administrators out or forge a holder. A package folder that did not exist (a first install) is created for the lock and
removed again, with the empty folders around the lock, when the run ends with nothing in it.
"""

from __future__ import annotations

import os
import socket
import tomllib
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..catalog.local import DATA_DIR_NAME, is_reparse_point
from ..system.port import System
from .executor import prune_empty_package

LOCK_NAME = "package.lock"
MAX_LOCK_AGE = timedelta(hours=24)
YOUNG = timedelta(seconds=10)


class PackageBusy(RuntimeError):
    """Another gupkg run holds the package (exit status 3: nothing was changed)."""

    code = "package-busy"

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Holder:
    """Who holds a package lock."""

    pid: int
    host: str
    operation: str
    started: str
    token: str

    def describe(self) -> str:
        """Return the holder in one sentence for a message."""
        return f"process {self.pid} on {self.host} ({self.operation}, started {self.started})"


def locks_dir(package_path: Path) -> Path:
    """Return the folder that holds the lock of a package."""
    return Path(package_path) / DATA_DIR_NAME / "locks"


def lock_path(package_path: Path) -> Path:
    """Return the lock file of a package."""
    return locks_dir(package_path) / LOCK_NAME


def _parse(text: str) -> Holder | None:
    """Read a lock file's text; ``None`` when it is not a valid lock."""
    try:
        data = tomllib.loads(text)
        return Holder(int(data["pid"]), str(data["host"]), str(data["operation"]), str(data["started"]), str(data["token"]))
    except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError):
        return None


def render_lock(holder: Holder) -> str:
    """Return the text of a lock file."""
    return (
        f"pid = {holder.pid}\nhost = {_quote(holder.host)}\noperation = {_quote(holder.operation)}\n"
        f"started = {_quote(holder.started)}\ntoken = {_quote(holder.token)}\n"
    )


def _quote(text: str) -> str:
    """Return *text* as a TOML basic string."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(char if char >= " " else " " for char in escaped) + '"'


def _read(path: Path) -> tuple[Holder | None, float | None]:
    """Return the holder written in *path* (``None`` when unreadable) and the file's modification time."""
    try:
        mtime = path.stat().st_mtime
        return _parse(path.read_text(encoding="utf-8")), mtime
    except (OSError, UnicodeDecodeError):
        return None, None


def _is_stale(system: System, holder: Holder | None, mtime: float | None, now: datetime) -> bool:
    """Return whether a lock no live run holds: dead process, too old, or unreadable and not brand new."""
    age = timedelta(seconds=max(0.0, now.timestamp() - mtime)) if mtime is not None else MAX_LOCK_AGE
    if age > MAX_LOCK_AGE:
        return True
    if holder is None:
        return age > YOUNG
    if holder.host.casefold() != socket.gethostname().casefold():
        return False
    return not system.process_is_running(holder.pid)


def lock_holder(system: System, package_path: Path, now: datetime) -> Holder | None:
    """Return the live holder of *package_path*'s lock, or ``None`` (no lock, or a stale one). Nothing is changed."""
    path = lock_path(package_path)
    if is_reparse_point(path.parent) or not path.is_file():
        return None
    holder, mtime = _read(path)
    if _is_stale(system, holder, mtime, now):
        return None
    return holder or Holder(0, "unknown", "unknown", "just now", "")


def _check_folder(system: System, package_path: Path, elevated: bool) -> Path:
    """Create the locks folder and judge it: never a link, and admin-only in an elevated run."""
    folder = locks_dir(package_path)
    data = folder.parent
    for entry in (Path(package_path), data):
        if is_reparse_point(entry):
            raise PackageBusy(f"{entry} is a link; refusing to keep a lock there")
    folder.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(folder):
        raise PackageBusy(f"{folder} is a link; refusing to keep a lock there")
    if elevated:
        reason = system.directory_untrusted_reason(folder)
        if reason is not None:
            raise PackageBusy(f"An elevated run keeps its lock only in an admin-only folder, and {folder} is not: {reason}")
    return folder


def _busy(holder: Holder | None, package_path: Path) -> PackageBusy:
    """Return the error for a package held by *holder*."""
    who = holder.describe() if holder is not None else "another gupkg run that is just starting"
    return PackageBusy(
        f"{package_path.name} is being changed by {who}. Wait for it to finish and run the command again.",
        f"If that run is gone, the lock ({lock_path(package_path)}) is removed by the next run once its process has ended.",
    )


def _create(path: Path, text: str) -> bool:
    """Create *path* exclusively with *text*; return whether this run created it."""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return True


def _reclaim(path: Path, seen: Holder | None) -> None:
    """Move a stale lock aside; put it back when what was moved turns out to be a different (live) lock."""
    aside = path.with_name(f"{path.name}.stale-{uuid.uuid4().hex}")
    try:
        os.replace(path, aside)
    except OSError:
        return
    moved, _ = _read(aside)
    if (moved.token if moved else None) != (seen.token if seen else None):
        # A live run took the lock between the check and the move: give it back (the move is not allowed to steal it).
        try:
            os.link(aside, path)
        except OSError:
            pass
    try:
        aside.unlink()
    except OSError:
        pass


def _release(path: Path, token: str, *, drop_data: bool) -> None:
    """Remove the lock file when it still carries *token*, the locks folder when it is empty and, when *drop_data*, an empty ``.gupkg``."""
    holder, _ = _read(path)
    if holder is not None and holder.token == token:
        try:
            path.unlink()
        except OSError:
            pass
    for folder in (path.parent, path.parent.parent if drop_data else None):
        if folder is None:
            continue
        try:
            folder.rmdir()
        except OSError:
            pass


@contextmanager
def package_lock(
    system: System, package_path: Path, operation: str, now: Callable[[], datetime], *, elevated: bool = False
) -> Iterator[Holder]:
    """Hold the lock of *package_path* for the block.

    Parameters
    ----------
    system : System
        Judges whether the holder of an existing lock is alive and whether an elevated run's folder is admin-only.
    package_path : Path
        The package root (created when it does not exist yet, and removed again when the run leaves it empty).
    operation : str
        What the run does, written in the lock for the message of a second run.
    now : callable
        The clock (UTC).
    elevated : bool
        Whether this run is elevated (the locks folder must then be admin-only).

    Yields
    ------
    Holder
        This run.

    Raises
    ------
    PackageBusy
        If a live run holds the package, or the locks folder cannot be trusted.
    """
    package_path = Path(package_path)
    existed = package_path.is_dir()
    data_existed = (package_path / DATA_DIR_NAME).exists()
    path = lock_path(package_path)
    me = Holder(system.process_id(), socket.gethostname(), operation, now().isoformat(timespec="seconds"), uuid.uuid4().hex)
    try:
        _check_folder(system, package_path, elevated)
        for _attempt in range(3):
            if _create(path, render_lock(me)):
                break
            holder, mtime = _read(path)
            if not _is_stale(system, holder, mtime, now()):
                raise _busy(holder, package_path)
            _reclaim(path, holder)
        else:
            raise _busy(None, package_path)
    except BaseException:
        _release(path, "", drop_data=not data_existed)
        if not existed:
            prune_empty_package(package_path)
        raise
    try:
        yield me
    finally:
        _release(path, me.token, drop_data=not data_existed)
        if not existed:
            prune_empty_package(package_path)

