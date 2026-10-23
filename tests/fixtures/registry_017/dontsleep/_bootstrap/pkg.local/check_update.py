"""Discover the newest DontSleep 64-bit portable ZIP from softwareok.com.

SoftwareOK publishes DontSleep under stable, unversioned file names. The
download page announces the current version in its description and lists the
SHA-256 digest of every ZIP, so this module reads both from that page.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, archive URL, and digest.

Implementation Approach
-----------------------
The download page is fetched once. The version comes from the "Download Don't
Sleep <version>" description and the digest from the row for
``DontSleep_x64_Portable.zip``.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_PAGE = "https://www.softwareok.com/?Download=DontSleep"
_FILE = "DontSleep_x64_Portable.zip"
_URL = "https://www.softwareok.com/Download/" + _FILE
_VERSION = re.compile(r"Download Don't Sleep (?P<version>\d+(?:\.\d+)*)")
_DIGEST = re.compile(re.escape(_FILE) + r"<div[^>]*>.*?>(?P<digest>[0-9a-fA-F]{64})<", re.DOTALL)


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest DontSleep x64 portable ZIP when it is not installed.

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
        The download page cannot be read or lacks a version.
    """
    # Identify as a browser; the site is a consumer download page.
    request = urllib.request.Request(_PAGE, headers={"User-Agent": "Mozilla/5.0 gupkg-dontsleep-check/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {_PAGE}: {exc}") from exc

    # The announced version is the only version information the site offers.
    match = _VERSION.search(page)
    if match is None:
        raise RuntimeError("DontSleep download page does not announce a version")
    version = match.group("version")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    candidate = {
        "candidateId": f"dontsleep:{version}",
        "version": version,
        "url": _URL,
        "fileName": _FILE,
    }
    # Attach the published digest only when the page lists one for this file.
    digest = _DIGEST.search(page)
    if digest is not None:
        candidate["sha256"] = digest.group("digest").lower()
    return candidate
