"""Discover the newest stable Julia 64-bit Windows ZIP.

The Julia project publishes a machine-readable index of every release and
build at ``julialang-s3.julialang.org/bin/versions.json``. This module picks
the highest release marked stable and returns its ``win64`` ZIP archive.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, archive URL, and digest.

Implementation Approach
-----------------------
The index maps version strings to ``stable`` flags and file lists. Entries
that are not stable (alpha, beta, release candidates) are ignored. Versions
are ordered by their numeric components, and the file for 64-bit Windows with
kind ``archive`` and extension ``zip`` supplies the URL and SHA-256 digest.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_INDEX_URL = "https://julialang-s3.julialang.org/bin/versions.json"
_STABLE_VERSION = re.compile(r"\d+\.\d+\.\d+")


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components suitable for ordering Julia versions."""
    return tuple(int(part) for part in version.split("."))


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest stable Julia win64 ZIP when it is not installed.

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
        The index cannot be read or lists no stable win64 ZIP.
    """
    try:
        with urllib.request.urlopen(_INDEX_URL, timeout=60) as response:
            index = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read the Julia version index: {exc}") from exc

    # Keep only stable releases that ship a 64-bit Windows ZIP archive.
    releases: dict[str, dict[str, Any]] = {}
    for version, entry in index.items():
        if not (isinstance(entry, dict) and entry.get("stable") is True):
            continue
        if not _STABLE_VERSION.fullmatch(version):
            continue
        for item in entry.get("files", []):
            if (
                isinstance(item, dict)
                and item.get("os") == "winnt"
                and item.get("arch") == "x86_64"
                and item.get("kind") == "archive"
                and item.get("extension") == "zip"
            ):
                releases[version] = item
                break
    if not releases:
        raise RuntimeError("Julia version index lists no stable win64 ZIP")

    version = max(releases, key=_version_key)
    item = releases[version]

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    candidate = {
        "candidateId": f"julia:{version}",
        "version": version,
        "url": item["url"],
        "fileName": item["url"].rsplit("/", 1)[-1],
    }
    digest = item.get("sha256")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        candidate["sha256"] = digest.lower()
    return candidate
