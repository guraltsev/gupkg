"""Discover the newest HWiNFO release from its public download page.

The HWiNFO download page marks its U.S. mirror link with the text 'Local (U.S.)'.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the discovered version and archive URL.

Implementation Approach
-----------------------
The download page is read once and every link matching the publisher's file
naming pattern is collected. The highest numeric version wins. The file name carries the version without a
decimal point (``854`` for 8.54). The site refuses downloads without a browser
User-Agent, so the candidate carries one.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_PAGE_URL = "https://www.hwinfo.com/download/"
_LINK = re.compile(r'''href="(?P<href>https://www\.hwinfo\.com/files/hwi_(?P<version>\d+)\.zip)"[^>]*>\s*Local \(U\.S\.\)''')
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gupkg-update-check/1",
    "Accept": "text/html,application/xhtml+xml,*/*",
}
_DOWNLOAD_HEADERS: dict[str, str] = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36", "Referer": "https://www.hwinfo.com/download/"}


def _read(url: str) -> str:
    """Return the text of one publisher page."""
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {url}: {exc}") from exc


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components suitable for ordering versions."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest HWiNFO archive when it is not installed.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the current package identity.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata, or ``None`` when the newest release is installed
        and its payload is healthy.

    Raises
    ------
    RuntimeError
        The download page cannot be read or lists no matching release.
    """
    # Collect every release link the page offers; the newest one is the
    # package's update candidate.
    matches = list(_LINK.finditer(_read(_PAGE_URL)))
    if not matches:
        raise RuntimeError("HWiNFO download page lists no matching release")
    match = max(matches, key=lambda item: _version_key(item.group("version")))
    version = f"{match.group('version')[0]}.{match.group('version')[1:]}"

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    file_name = f"hwi_{match.group('version')}.zip"
    return {
        "candidateId": f"hwinfo:{version}:{file_name}",
        "version": version,
        "url": match.group("href"),
        "fileName": file_name,
        **({"headers": dict(_DOWNLOAD_HEADERS)} if _DOWNLOAD_HEADERS else {}),
    }
