"""Discover the current Sysinternals Process Explorer build from its download's modification date.

Sysinternals publishes one unversioned ZIP, so its modification date is the version.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate uses the build date as its version.

Implementation Approach
-----------------------
A HEAD request reads the file's ``Last-Modified`` header. Its UTC date, written
``YYYYMMDD``, becomes the package version, so a republished file produces a
newer version directory.
"""

from __future__ import annotations

import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any


PKG_MODULE_API = 1

_URL = "https://download.sysinternals.com/files/ProcessExplorer.zip"
_FILE_NAME = "ProcessExplorer.zip"
_HEADERS = {"User-Agent": "gupkg-update-check/1"}


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the current Sysinternals Process Explorer download when it is not installed.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the current package identity.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata, or ``None`` when this build is installed and its
        payload is healthy.

    Raises
    ------
    RuntimeError
        The download cannot be reached or reports no modification date.
    """
    request = urllib.request.Request(_URL, method="HEAD", headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            modified = response.headers.get("Last-Modified")
    except OSError as exc:
        raise RuntimeError(f"Could not read the Sysinternals Process Explorer download: {exc}") from exc
    if not modified:
        raise RuntimeError("Sysinternals Process Explorer download reports no Last-Modified date")
    version = parsedate_to_datetime(modified).strftime("%Y%m%d")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    return {
        "candidateId": f"processexplorer:{version}",
        "version": version,
        "url": _URL,
        "fileName": _FILE_NAME,
    }
