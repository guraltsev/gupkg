"""Discover the newest Inkscape 64-bit portable 7-Zip release.

Inkscape's site reaches a file through three pages: the release list, the
version's download page, and a short redirect page that names the file. This
module follows them and reads the SHA-256 digest the publisher posts beside
the file.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, file URL, and digest.

Implementation Approach
-----------------------
The highest numeric release link is chosen from the release list. Its
``compressed-7z`` download page carries a meta-refresh to the file and a link
to the digest file. The site refuses long browser User-Agent strings, so every
request, including the download itself, sends the short ``Mozilla/5.0`` form.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_SITE = "https://inkscape.org"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_ATTEMPTS = 6
_RELEASE_LINK = re.compile(r'href="(?:/[a-z]{2}(?:-[a-z]+)?)?/release/inkscape-(?P<version>\d+(?:\.\d+)+)/"')
_FILE_LINK = re.compile(r'href="(?P<path>/gallery/item/\d+/[^"]+-x64[^"]*\.7z)"')
_DIGEST_LINK = re.compile(r'href="(?P<url>https://[^"]+-x64\.7z[^"]*\.sha256)"')
_SHA256 = re.compile(r"\b(?P<digest>[0-9a-fA-F]{64})\b")


def _read(url: str) -> str:
    """Return the text of one Inkscape page."""
    request = urllib.request.Request(url, headers=_HEADERS)
    for attempt in range(_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read().decode("utf-8", errors="replace")
        except OSError as exc:
            # The site answers intermittently with gateway errors, so a
            # failed read is retried before the check gives up.
            if attempt + 1 == _ATTEMPTS:
                raise RuntimeError(f"Could not read {url}: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components suitable for ordering versions."""
    return tuple(int(part) for part in version.split("."))


def check_update(context: dict[str, Any]) -> dict[str, Any] | None:
    """Return the newest Inkscape 7-Zip archive when it is not installed.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the current package identity.

    Returns
    -------
    dict[str, Any] | None
        Candidate metadata, or ``None`` when the newest release is installed
        and its payload is healthy.

    Raises
    ------
    RuntimeError
        An Inkscape page cannot be read or lacks the expected release, file,
        or digest link.
    """
    # Choose the newest numeric release from the public release list.
    versions = {m.group("version") for m in _RELEASE_LINK.finditer(_read(f"{_SITE}/release/"))}
    if not versions:
        raise RuntimeError("Inkscape release list contains no numeric versions")
    version = max(versions, key=_version_key)

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    # The download page redirects to the file and links its digest.
    page = _read(f"{_SITE}/release/inkscape-{version}/windows/64-bit/compressed-7z/dl/")
    file_link = _FILE_LINK.search(page)
    if file_link is None:
        raise RuntimeError(f"Inkscape {version} download page names no 64-bit 7-Zip file")
    path = file_link.group("path")
    candidate: dict[str, Any] = {
        "candidateId": f"inkscape:{version}",
        "version": version,
        "url": _SITE + path,
        "fileName": urllib.parse.unquote(path.rsplit("/", 1)[-1]),
        "headers": dict(_HEADERS),
    }

    # Publish the digest when the site posts one beside the file.
    digest_link = _DIGEST_LINK.search(page)
    if digest_link is not None:
        digest = _SHA256.search(_read(digest_link.group("url")))
        if digest is not None:
            candidate["sha256"] = digest.group("digest").lower()
    return candidate
