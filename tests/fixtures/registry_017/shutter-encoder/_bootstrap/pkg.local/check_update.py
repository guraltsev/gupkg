"""Discover the current Shutter Encoder portable Windows ZIP.

The Shutter Encoder home page offers the portable build as a download link
whose key is part of the page. The version is read from the file name the
server reports for that link, for example
``Shutter Encoder 20.4 Windows 64bits.zip``.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version and download URL.

Implementation Approach
-----------------------
The home page is scanned for download links, and each link is probed with a
``HEAD`` request until one reports a Windows ``.zip`` file name. Probing is
needed because the page labels links but does not state file names.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_PAGE = "https://www.shutterencoder.com/"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
_LINK = re.compile(r'https://www\.shutterencoder\.com/sdc_download/\d+/\?key=[A-Za-z0-9]+')
_FILE = re.compile(r'filename="?(?P<name>Shutter Encoder (?P<version>\d+(?:\.\d+)*) Windows 64bits\.zip)"?')


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the current Shutter Encoder ZIP when it is not installed.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the current package identity.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata, or ``None`` when the offered release is installed
        and its payload is healthy.

    Raises
    ------
    RuntimeError
        The page cannot be read or offers no Windows portable ZIP.
    """
    headers = {"User-Agent": _USER_AGENT}
    try:
        with urllib.request.urlopen(urllib.request.Request(_PAGE, headers=headers), timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {_PAGE}: {exc}") from exc

    # Probe each distinct download link for a Windows ZIP file name.
    for link in dict.fromkeys(_LINK.findall(text)):
        probe = urllib.request.Request(link, headers=headers, method="HEAD")
        try:
            with urllib.request.urlopen(probe, timeout=30) as response:
                disposition = response.headers.get("Content-Disposition", "")
        except OSError:
            continue
        match = _FILE.search(disposition)
        if match:
            break
    else:
        raise RuntimeError("Shutter Encoder page offers no Windows 64-bit ZIP")

    version = match.group("version")
    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None
    return {
        "candidateId": f"shutter-encoder:{version}",
        "version": version,
        "url": link,
        "fileName": match.group("name"),
        "headers": headers,
    }
