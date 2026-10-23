"""Discover the newest Graphviz release from its public download page.

The Graphviz download page links each release's Windows 64-bit ZIP on GitLab.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the discovered version and archive URL.

Implementation Approach
-----------------------
The download page is read once and every link matching the publisher's file
naming pattern is collected. The highest numeric version wins.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_PAGE_URL = "https://graphviz.org/download/"
_LINK = re.compile(r"graphviz-releases/(?P<version>\d+(?:\.\d+)+)/windows_10_cmake_Release_Graphviz-(?P=version)-win64\.zip")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gupkg-update-check/1",
    "Accept": "text/html,application/xhtml+xml,*/*",
}
_DOWNLOAD_HEADERS: dict[str, str] = {}


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
    """Return the newest Graphviz archive when it is not installed.

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
        raise RuntimeError("Graphviz download page lists no matching release")
    match = max(matches, key=lambda item: _version_key(item.group("version")))
    version = match.group("version")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    file_name = f"windows_10_cmake_Release_Graphviz-{version}-win64.zip"
    return {
        "candidateId": f"graphviz:{version}:{file_name}",
        "version": version,
        "url": f"https://gitlab.com/api/v4/projects/4207231/packages/generic/graphviz-releases/{version}/{file_name}",
        "fileName": file_name,
        **({"headers": dict(_DOWNLOAD_HEADERS)} if _DOWNLOAD_HEADERS else {}),
    }
