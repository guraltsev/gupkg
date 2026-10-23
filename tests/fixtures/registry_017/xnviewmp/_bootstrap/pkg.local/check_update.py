"""Discover the newest XnView MP 64-bit ZIP from xnview.com.

XnView MP is downloaded from an unversioned address, so the version is read
from the product page, which announces the current release.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version and archive URL.

Implementation Approach
-----------------------
The product page embeds structured data with a ``softwareVersion`` field. The
unversioned ``XnViewMP-win-x64.zip`` URL is paired with that version. The
publisher provides no digest.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_PAGE = "https://www.xnview.com/en/xnview-mp/"
_URL = "https://download.xnview.com/XnViewMP-win-x64.zip"
_VERSION = re.compile(r'"softwareVersion":\s*"(?P<version>\d+(?:\.\d+)*)"')


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest XnView MP 64-bit ZIP when it is not installed.

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
        The product page cannot be read or announces no version.
    """
    request = urllib.request.Request(_PAGE, headers={"User-Agent": "Mozilla/5.0 gupkg-xnviewmp-check/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {_PAGE}: {exc}") from exc

    # The structured-data version is the release the unversioned ZIP serves.
    match = _VERSION.search(page)
    if match is None:
        raise RuntimeError("XnView MP product page announces no version")
    version = match.group("version")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    return {
        "candidateId": f"xnviewmp:{version}",
        "version": version,
        "url": _URL,
        "fileName": "XnViewMP-win-x64.zip",
    }
