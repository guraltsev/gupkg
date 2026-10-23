"""Discover the newest stable PortableGit 64-bit release from Git for Windows.

Git for Windows tags releases such as ``v2.46.2.windows.1``. The portable
archive is named after the Git version only, for example
``PortableGit-2.46.2-64-bit.7z.exe``, with the Windows rebuild number added
for rebuilds (``PortableGit-2.46.2.2-64-bit.7z.exe``). This module reads the
latest release and selects that asset.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, archive URL, and digest.

Implementation Approach
-----------------------
The GitHub latest-release endpoint is queried, which excludes drafts and
prereleases. The version is taken from the asset name so it always matches the
downloaded file.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_API_URL = "https://api.github.com/repos/git-for-windows/git/releases/latest"
_ASSET = re.compile(r"PortableGit-(?P<version>\d+(?:\.\d+)*)-64-bit\.7z\.exe")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest PortableGit 64-bit archive when it is not installed.

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
        The release cannot be read or has no PortableGit 64-bit asset.
    """
    request = urllib.request.Request(
        _API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "gupkg-portablegit-check/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read the latest Git for Windows release: {exc}") from exc

    # Select the single uploaded PortableGit 64-bit self-extracting archive.
    matches = [
        (asset, match)
        for asset in release.get("assets", [])
        if isinstance(asset, dict)
        and asset.get("state") == "uploaded"
        and (match := _ASSET.fullmatch(str(asset.get("name"))))
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Latest Git for Windows release must contain one PortableGit 64-bit "
            f"asset; found {len(matches)}"
        )
    asset, match = matches[0]
    version = match.group("version")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    candidate = {
        "candidateId": f"git-for-windows:{release.get('id')}:{version}",
        "version": version,
        "url": asset["browser_download_url"],
        "fileName": asset["name"],
    }
    digest = asset.get("digest")
    if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        candidate["sha256"] = digest.removeprefix("sha256:").lower()
    return candidate
