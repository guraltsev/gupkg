"""Discover the newest stable GIMP Windows installer from gimp.org.

GIMP publishes installers under ``download.gimp.org/gimp/v<major>.<minor>/windows/``
with a ``SHA256SUMS`` file beside them. Odd minor series are development
releases and are ignored, as are release candidates.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, installer URL, and digest.

Implementation Approach
-----------------------
The newest even-minor series directory is found from the top-level index, its
Windows index is searched for ``gimp-<version>-setup.exe`` files, and the
highest version is paired with its digest from ``SHA256SUMS``.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_ROOT = "https://download.gimp.org/gimp/"
_SERIES = re.compile(r'href="v(?P<major>\d+)\.(?P<minor>\d+)/"')
_SETUP = re.compile(r'href="gimp-(?P<version>\d+(?:\.\d+)*)-setup\.exe"')
_SHA256_LINE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64}) [ *]?(?P<name>\S+)$", re.MULTILINE)


def _read(url: str) -> str:
    """Return the text of one gimp.org page or checksum file."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {url}: {exc}") from exc


def _key(version: str) -> tuple[int, ...]:
    """Return numeric components suitable for ordering GIMP versions."""
    return tuple(int(part) for part in version.split("."))


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest stable GIMP installer when it is not installed.

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
        A gimp.org page cannot be read or lists no stable installer.
    """
    # Walk stable (even-minor) series from newest to oldest until one has an installer.
    series = sorted(
        {(int(m["major"]), int(m["minor"])) for m in _SERIES.finditer(_read(_ROOT))},
        reverse=True,
    )
    for major, minor in series:
        if minor % 2:
            continue
        directory = f"{_ROOT}v{major}.{minor}/windows/"
        try:
            listing = _read(directory)
        except RuntimeError:
            continue
        versions = {m["version"] for m in _SETUP.finditer(listing)}
        if versions:
            break
    else:
        raise RuntimeError("download.gimp.org lists no stable Windows installer")
    version = max(versions, key=_key)

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    file_name = f"gimp-{version}-setup.exe"
    candidate = {
        "candidateId": f"gimp:{version}",
        "version": version,
        "url": directory + file_name,
        "fileName": file_name,
    }
    sums = _read(directory + "SHA256SUMS")
    digest = next((m["digest"] for m in _SHA256_LINE.finditer(sums) if m["name"] == file_name), None)
    if digest is not None:
        candidate["sha256"] = digest.lower()
    return candidate
