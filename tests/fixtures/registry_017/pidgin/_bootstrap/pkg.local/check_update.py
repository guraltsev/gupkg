"""Discover the newest Pidgin offline installer for Windows.

Pidgin publishes Windows builds on SourceForge, one folder per release, each
holding ``pidgin-<version>-offline.exe`` that bundles the GTK runtime. No
true portable edition exists, so the package unpacks that installer.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, installer URL, and file name.

Implementation Approach
-----------------------
The SourceForge folder listing for ``Pidgin`` is parsed for version folders,
which are ordered by their numeric components. The newest folder whose listing
names the offline installer is chosen.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_ROOT = "https://sourceforge.net/projects/pidgin/files/Pidgin/"
_VERSION_FOLDER = re.compile(r"Pidgin/(?P<version>\d+(?:\.\d+)+)/")


def _read(url: str) -> str:
    """Return the text of one SourceForge listing page."""
    request = urllib.request.Request(url, headers={"User-Agent": "gupkg-pidgin-check/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {url}: {exc}") from exc


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components suitable for ordering Pidgin versions."""
    return tuple(int(part) for part in version.split("."))


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest Pidgin offline installer when it is not installed.

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
        A SourceForge page cannot be read or no release has an offline
        installer.
    """
    versions = sorted(
        {m.group("version") for m in _VERSION_FOLDER.finditer(_read(_ROOT))},
        key=_version_key,
        reverse=True,
    )

    # Walk from the newest folder down until one lists the offline installer.
    for version in versions:
        file_name = f"pidgin-{version}-offline.exe"
        folder = f"{_ROOT}{version}/"
        if f"/{version}/{file_name}" not in _read(folder):
            continue

        current = context.get("current", {})
        if current.get("version") == version and current.get("appReady", True):
            return None
        return {
            "candidateId": f"pidgin:{version}",
            "version": version,
            "url": f"{folder}{file_name}/download",
            "fileName": file_name,
        }
    raise RuntimeError("SourceForge lists no Pidgin offline installer")
