"""Discover the newest WuMgr release asset from GitHub.

The release tag (``v1.1``) differs from the asset name (``WuMgr_v1.1b.zip``).

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the release version and asset URL.

Implementation Approach
-----------------------
GitHub's latest-release record supplies the version from its tag. The asset is
selected by a regular expression so a file name that does not repeat the tag
exactly still resolves to one unambiguous download.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_RELEASE_URL = "https://api.github.com/repos/DavidXanatos/wumgr/releases/latest"
_ASSET = re.compile(r"WuMgr_v[\d.]+[a-z]?\.zip")


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the latest WuMgr asset when it is not installed.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the current package identity.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata, or ``None`` when the latest release is installed
        and its payload is healthy.

    Raises
    ------
    RuntimeError
        The release cannot be read or does not contain exactly one asset
        matching the expected name.
    """
    request = urllib.request.Request(
        _RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "gupkg-wumgr-update-check/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read the WuMgr release: {exc}") from exc

    # Validate the fields the candidate depends on before using them.
    tag = release.get("tag_name") if isinstance(release, dict) else None
    release_id = release.get("id") if isinstance(release, dict) else None
    if not isinstance(tag, str) or not isinstance(release_id, int):
        raise RuntimeError("WuMgr release has no valid tag or release ID")
    version = re.sub(r"^[vV](?=\d)", "", tag)

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    # Require one matching uploaded asset so a similarly named file cannot
    # become the application payload.
    assets = release.get("assets")
    matches = [
        asset
        for asset in (assets if isinstance(assets, list) else [])
        if isinstance(asset, dict)
        and asset.get("state") == "uploaded"
        and isinstance(asset.get("name"), str)
        and _ASSET.fullmatch(asset["name"])
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"WuMgr release must contain exactly one asset matching "
            f"{_ASSET.pattern!r}; found {len(matches)}"
        )
    asset = matches[0]
    url = asset.get("browser_download_url")
    if not isinstance(url, str):
        raise RuntimeError("WuMgr asset has no download URL")

    candidate = {
        "candidateId": f"wumgr:{release_id}",
        "version": version,
        "url": url,
        "fileName": asset["name"],
    }
    digest = asset.get("digest")
    if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        candidate["sha256"] = digest.removeprefix("sha256:")
    return candidate
