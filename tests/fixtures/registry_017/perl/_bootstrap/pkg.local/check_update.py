"""Discover the newest Strawberry Perl 64-bit portable ZIP.

Strawberry Perl releases are tagged like ``SP_54231_64bit``, which does not
spell out the version, so the built-in GitHub check cannot be used. The
portable ZIP is named after the version, for example
``strawberry-perl-5.42.3.1-64bit-portable.zip``.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, archive URL, and digest.

Implementation Approach
-----------------------
The GitHub latest-release endpoint is queried, which excludes drafts and
prereleases. The version is taken from the asset name so it always matches
the downloaded file; the asset's published SHA-256 digest is passed along.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_API_URL = "https://api.github.com/repos/StrawberryPerl/Perl-Dist-Strawberry/releases/latest"
_ASSET = re.compile(r"strawberry-perl-(?P<version>\d+(?:\.\d+)+)-64bit-portable\.zip")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest Strawberry Perl portable ZIP when it is not installed.

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
        The release cannot be read or has no portable 64-bit ZIP.
    """
    request = urllib.request.Request(
        _API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "gupkg-perl-check/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read the latest Strawberry Perl release: {exc}") from exc

    # Select the single uploaded 64-bit portable ZIP (not the PDL variant).
    matches = [
        (asset, match)
        for asset in release.get("assets", [])
        if isinstance(asset, dict)
        and asset.get("state") == "uploaded"
        and (match := _ASSET.fullmatch(str(asset.get("name"))))
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Latest Strawberry Perl release must contain one 64-bit portable ZIP; "
            f"found {len(matches)}"
        )
    asset, match = matches[0]
    version = match.group("version")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    candidate = {
        "candidateId": f"strawberry-perl:{release.get('id')}:{version}",
        "version": version,
        "url": asset["browser_download_url"],
        "fileName": asset["name"],
    }
    digest = asset.get("digest")
    if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        candidate["sha256"] = digest.removeprefix("sha256:").lower()
    return candidate
