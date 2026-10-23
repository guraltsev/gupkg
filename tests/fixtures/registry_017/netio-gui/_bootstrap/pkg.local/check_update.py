"""Discover the newest NetIO-GUI portable ZIP on SourceForge.

NetIO-GUI releases are files named like ``netio-gui_v2.0.0.38_portable.zip``
in the root of the ``netiogui`` SourceForge project. This module selects the
highest version.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version and archive URL.

Implementation Approach
-----------------------
The project's file RSS feed is parsed for portable ZIP names. SourceForge
rejects non-browser clients, so a browser-style ``User-Agent`` is sent for the feed only; downloads must use the default one.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_FEED = "https://sourceforge.net/projects/netiogui/rss?path=/"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
_ARCHIVE = re.compile(r"/(?P<name>netio-gui_v(?P<version>\d+(?:\.\d+)*)_portable\.zip)")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest NetIO-GUI portable ZIP when it is not installed.

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
        The feed cannot be read or lists no portable ZIP.
    """
    request = urllib.request.Request(_FEED, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {_FEED}: {exc}") from exc

    names = {m.group("version"): m.group("name") for m in _ARCHIVE.finditer(text)}
    if not names:
        raise RuntimeError("The NetIO-GUI feed lists no portable ZIP")
    version = max(names, key=lambda v: tuple(int(p) for p in v.split(".")))

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None
    file_name = names[version]
    return {
        "candidateId": f"netio-gui:{version}",
        "version": version,
        "url": f"https://sourceforge.net/projects/netiogui/files/{file_name}/download",
        "fileName": file_name,
    }
