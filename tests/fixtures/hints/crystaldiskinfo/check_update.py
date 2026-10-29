"""Discover the current CrystalDiskInfo release by following its download link.

The publisher's stable download address redirects to the newest portable ZIP on SourceForge.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version and the resolved file URL.

Implementation Approach
-----------------------
The publisher's stable download address answers with redirects. They are
followed one hop at a time without downloading the file; the final file name
carries the version number. A redirect that wraps its target in a ``url=``
query parameter is unwrapped so the real file address is used.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_START_URL = "https://crystalmark.info/redirect.php?product=CrystalDiskInfo"
_VERSION = re.compile(r"/files/(?P<version>\d+(?:\.\d+)+)/")
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gupkg-update-check/1"}
_MAX_HOPS = 6
_ATTEMPTS = 4


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report redirects to the caller instead of following them."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _resolve(url: str) -> str:
    """Follow redirects from ``url`` and return the final file address."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(_MAX_HOPS):
        location = None
        for attempt in range(_ATTEMPTS):
            request = urllib.request.Request(url, method="HEAD", headers=_HEADERS)
            try:
                with opener.open(request, timeout=30):
                    return url
            except urllib.error.HTTPError as exc:
                # Some publisher backends answer intermittently with a server
                # error, so only those responses are retried.
                if exc.code >= 500 and attempt + 1 < _ATTEMPTS:
                    continue
                location = exc.headers.get("Location")
                if exc.code not in (301, 302, 303, 307, 308) or not location:
                    raise RuntimeError(f"Could not resolve {url}: HTTP {exc.code}") from exc
                break
            except OSError as exc:
                raise RuntimeError(f"Could not resolve {url}: {exc}") from exc
        url = urllib.parse.urljoin(url, location)
        # A wrapper such as an advertising redirect carries the real target.
        wrapped = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("url")
        if wrapped:
            return wrapped[0]
    raise RuntimeError(f"Too many redirects while resolving {_START_URL}")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the current CrystalDiskInfo file when it is not installed.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the current package identity.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata, or ``None`` when the resolved version is installed
        and its payload is healthy.

    Raises
    ------
    RuntimeError
        The download link cannot be resolved or its address carries no version.
    """
    url = _resolve(_START_URL)
    match = _VERSION.search(url)
    if match is None:
        raise RuntimeError(f"CrystalDiskInfo download address has no version: {url}")
    version = match.group("version")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    file_name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
    if file_name == "download":
        file_name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 2)[-2])
    return {
        "candidateId": f"crystaldiskinfo:{version}:{file_name}",
        "version": version,
        "url": url,
        "fileName": file_name,
    }
