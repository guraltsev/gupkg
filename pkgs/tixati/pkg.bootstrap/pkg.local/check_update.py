"""Discover the newest Tixati portable edition ZIP from tixati.com.

Tixati names its portable download after the version, for example
``tixati-3.44-1.portable.zip``. This module reads the Windows download page,
which lists the current file names.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version and archive URL.

Implementation Approach
-----------------------
The Windows download page is scanned for the portable ZIP link. The version is
taken from the file name so it always matches the downloaded archive. Tixati
publishes GPG signatures only, so no digest is attached.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_PAGE = "https://tixati.com/windows"
_FILE = re.compile(r"tixati-(?P<version>\d+(?:\.\d+)*-\d+)\.portable\.zip")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest Tixati portable ZIP when it is not installed.

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
        The download page cannot be read or lists no portable ZIP.
    """
    request = urllib.request.Request(_PAGE, headers={"User-Agent": "Mozilla/5.0 gupkg-tixati-check/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {_PAGE}: {exc}") from exc

    # The page lists exactly one portable edition; its name carries the version.
    match = _FILE.search(page)
    if match is None:
        raise RuntimeError("Tixati Windows page lists no portable ZIP")
    version = match.group("version")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    return {
        "candidateId": f"tixati:{version}",
        "version": version,
        "url": f"https://download.tixati.com/{match.group(0)}",
        "fileName": match.group(0),
    }
