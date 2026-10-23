"""Download URLs to files while reporting download progress.

The helper (``download_file``) is shared by package origins, update payloads and
the registry, so human-facing workflows expose the expected size when the server
provides it and still report received bytes when the server omits
``Content-Length``. Downloaded files are verified with the streaming
``file_sha256(...)`` digest.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Mapping

from .core import log_info, log_progress
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
    def report(received: int, total: int | None, *, final: bool = False) -> None:
        """Show one progress line in the known-size or unknown-size form."""
        if total is None:
            text = f"{label}: {_format_bytes(received)} received"
        else:
            percentage = min(received / total * 100, 100) if total else 100
            text = (
                f"{label}: {_format_bytes(received)} / {_format_bytes(total)} "
                f"({percentage:.0f}%)"
            )
        log_progress(text, final=final)

    # Report by elapsed time, not by size, so slow and fast connections both
    # update smoothly; the first call announces the size, and the final report
    # is always committed as a lasting line.
    state = {"last_report": time.monotonic(), "total": None}

    def on_progress(received: int, total: int | None) -> None:
        """Announce the size once, then report at most every ``_REPORT_INTERVAL`` seconds."""
        state["total"] = total
        if received == 0:
            if total is None:
                log_info(f"{label}: total size unknown")
            else:
                log_info(f"{label}: total size {_format_bytes(total)}")
                report(0, total)
            return
        now = time.monotonic()
        if now - state["last_report"] >= _REPORT_INTERVAL:
            report(received, total)
            state["last_report"] = now

    received = system.fetch(url, headers, destination, on_progress)
    report(received, state["total"], final=True)
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


def _format_bytes(value: int) -> str:
    """Render bytes compactly for terminal progress lines."""
    if value < 1024:
        return f"{value} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB"):
        amount /= 1024
        if amount < 1024:
            return f"{amount:.1f} {unit}"
    return f"{amount / 1024:.1f} TiB"
