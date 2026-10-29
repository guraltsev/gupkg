"""Discover the newest restic-backup-gx Windows executable from GitLab.

restic-backup-gx is published as GitLab releases of the project
``stormking/resticguigx``. Each release links a Windows executable whose job
artifact path changes with every build, so the link is read from the release
rather than constructed.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version and executable URL.

Implementation Approach
-----------------------
The GitLab releases API lists releases newest first. The first release with a
link named ``Windows`` and a ``.exe`` target is selected, and its tag (without
the leading ``v``) becomes the version.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_API_URL = "https://gitlab.com/api/v4/projects/stormking%2Fresticguigx/releases"
_TAG = re.compile(r"v?(?P<version>\d+(?:\.\d+)*)")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest restic-backup-gx executable when it is not installed.

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
        The releases cannot be read or none provides a Windows executable.
    """
    request = urllib.request.Request(
        _API_URL, headers={"User-Agent": "gupkg-restic-backup-gx-check/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            releases = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read the restic-backup-gx releases: {exc}") from exc

    # Take the first (newest) release that links a Windows executable.
    match = None
    urls: list[str] = []
    for release in releases if isinstance(releases, list) else []:
        match = _TAG.fullmatch(str(release.get("tag_name")))
        links = release.get("assets", {}).get("links", [])
        urls = [
            link.get("direct_asset_url") or link.get("url")
            for link in links
            if isinstance(link, dict) and link.get("name") == "Windows"
        ]
        urls = [url for url in urls if isinstance(url, str) and url.lower().endswith(".exe")]
        if match and urls:
            break
    else:
        raise RuntimeError("No restic-backup-gx release provides a Windows executable")

    version = match.group("version")
    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None
    return {
        "candidateId": f"restic-backup-gx:{version}",
        "version": version,
        "url": urls[0],
        "fileName": urllib.parse.unquote(urllib.parse.urlparse(urls[0]).path).rsplit("/", 1)[-1],
    }
