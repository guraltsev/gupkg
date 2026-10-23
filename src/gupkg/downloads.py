"""Download URLs to files while reporting download progress as events.

The helper (``download_file``) is shared by package origins, update payloads and
the registry. It emits ``Progress`` events with the expected size when the server
provides it, and with received bytes only when the server omits
``Content-Length``; the last event of every download has ``done == total``. Downloaded files are verified with the streaming
``file_sha256(...)`` digest.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Mapping

from .report.events import Progress, emit
from .system.port import System


# Hashing granularity.
_CHUNK_SIZE = 1024 * 1024
# Minimum seconds between intermediate progress reports.
_REPORT_INTERVAL = 0.2


def download_file(
    system: System,
    url: str,
    destination: Path,
    *,
    label: str,
    headers: Mapping[str, str] | None = None,
) -> int:
    """Download one URL to a file through the system boundary, reporting transfer progress.

    Parameters
    ----------
    system : System
        Boundary that performs the transfer.
    url : str
        Address to download.
    destination : pathlib.Path
        File that receives the body.
    label : str
        Human-readable operation name included in progress messages.
    headers : mapping, optional
        Extra request headers.

    Returns
    -------
    int
        Number of bytes written to ``destination``.
    """
    # Report by elapsed time, not by size, so slow and fast connections both
    # update smoothly; the first call announces the size (or its absence).
    state = {"last_report": time.monotonic(), "completed": False}

    def on_progress(received: int, total: int | None) -> None:
        """Announce the size once, then report at most every ``_REPORT_INTERVAL`` seconds."""
        if received == 0:
            emit(Progress(label, 0, total))
            return
        now = time.monotonic()
        if now - state["last_report"] >= _REPORT_INTERVAL:
            emit(Progress(label, received, total))
            state["last_report"] = now
            state["completed"] = total is not None and received >= total

    # The last event always has ``done == total`` (the size is known by now),
    # unless the throttled reports already ended with exactly that event.
    received = system.fetch(url, headers, destination, on_progress)
    if not state["completed"]:
        emit(Progress(label, received, received))
    return received


def file_sha256(path: Path) -> str:
    """Return the lower-case hexadecimal SHA-256 digest of one file.

    Parameters
    ----------
    path : pathlib.Path
        File to hash in fixed-size chunks.

    Returns
    -------
    str
        The 64-character digest.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as file_handle:
        while chunk := file_handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()
