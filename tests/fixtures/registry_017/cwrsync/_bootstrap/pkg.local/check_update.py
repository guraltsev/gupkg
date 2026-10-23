"""Discover the newest free cwRsync 64-bit client ZIP from itefix.

The itefix download page links every published free client archive, named
like ``cwrsync_6.5.0_x64_free.zip``. This module selects the highest version.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version and archive URL.

Implementation Approach
-----------------------
The page at ``https://itefix.net/cwrsync`` is searched for archive links and
the versions are ordered by their numeric components.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_PAGE = "https://itefix.net/cwrsync"
_ARCHIVE = re.compile(r"/downloads/cwrsync/cwrsync_(?P<version>\d+(?:\.\d+)*)_x64_free\.zip")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest cwRsync client ZIP when it is not installed.

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
        The page cannot be read or links no client archive.
    """
    request = urllib.request.Request(_PAGE, headers={"User-Agent": "gupkg-cwrsync-check/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {_PAGE}: {exc}") from exc

    versions = {m.group("version") for m in _ARCHIVE.finditer(text)}
    if not versions:
        raise RuntimeError(f"{_PAGE} links no cwRsync x64 free archive")
    version = max(versions, key=lambda v: tuple(int(p) for p in v.split(".")))

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None
    file_name = f"cwrsync_{version}_x64_free.zip"
    return {
        "candidateId": f"cwrsync:{version}",
        "version": version,
        "url": f"https://itefix.net/downloads/cwrsync/{file_name}",
        "fileName": file_name,
    }
