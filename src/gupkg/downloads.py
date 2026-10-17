"""Copy HTTP response bodies to files while reporting download progress.

The helper is shared by package origins and update payloads so human-facing
workflows expose the expected size when the server provides it and still report
received bytes when the server omits ``Content-Length``. Downloaded files are
verified with the streaming ``file_sha256(...)`` digest.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from .core import log_info, log_progress


# Hashing granularity, and the transfer granularity of every download.
_CHUNK_SIZE = 1024 * 1024
_TRANSFER_CHUNK_SIZE = 64 * 1024
# Minimum seconds between intermediate progress reports.
_REPORT_INTERVAL = 0.2


def download_response(response: Any, destination: Path, *, label: str) -> int:
    """Write one HTTP response to a file and report its transfer progress.

    Parameters
    ----------
    response : object
        File-like binary response exposing ``read`` and optionally HTTP headers.
    destination : pathlib.Path
        File that receives the response body.
    label : str
        Human-readable operation name included in progress messages.

    Returns
    -------
    int
        Number of bytes written to ``destination``.
    """
    total = _content_length(response)

    def report(received: int, *, final: bool = False) -> None:
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

    if total is None:
        log_info(f"{label}: total size unknown")
    else:
        log_info(f"{label}: total size {_format_bytes(total)}")
        report(0)

    # Report by elapsed time, not by size, so slow and fast connections both
    # update smoothly; the final report is always committed as a lasting line.
    received = 0
    last_report = time.monotonic()
    with open(destination, "wb") as file_handle:
        while chunk := response.read(_TRANSFER_CHUNK_SIZE):
            file_handle.write(chunk)
            received += len(chunk)
            now = time.monotonic()
            if now - last_report >= _REPORT_INTERVAL:
                report(received)
                last_report = now
    report(received, final=True)
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


def _content_length(response: Any) -> int | None:
    """Return a valid response size when the HTTP boundary exposes one."""
    headers = getattr(response, "headers", None)
    raw_value = headers.get("Content-Length") if headers is not None else None
    if raw_value is None:
        getheader = getattr(response, "getheader", None)
        if callable(getheader):
            raw_value = getheader("Content-Length")
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


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
