"""Discover the newest stable Python embeddable ZIP for 64-bit Windows.

python.org publishes one folder per release under ``/ftp/python/``. Folders
for upcoming releases exist before any stable Windows build does, and folders
for stable releases also hold alpha, beta, and release-candidate files. This
module picks the highest release whose folder contains a final embeddable
``amd64`` ZIP.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, archive URL, and file name.

Implementation Approach
-----------------------
The FTP directory indexes are parsed for link names. Only versions made of
digits and dots count as stable, and the exact file name must be listed, so
``python-3.15.0rc3-embed-amd64.zip`` never matches. Versions are compared by
their numeric components.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_ROOT = "https://www.python.org/ftp/python/"
_VERSION_DIRECTORY = re.compile(r'href="(?P<version>3\.\d+\.\d+)/"')


def _read(url: str) -> str:
    """Return the text of one python.org directory index."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {url}: {exc}") from exc


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components suitable for ordering Python versions."""
    return tuple(int(part) for part in version.split("."))


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest stable Python embeddable ZIP when it is not installed.

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
        A python.org page cannot be read or no release has a stable
        embeddable ZIP.
    """
    versions = sorted(
        {m.group("version") for m in _VERSION_DIRECTORY.finditer(_read(_ROOT))},
        key=_version_key,
        reverse=True,
    )

    # Walk from the newest folder down; the newest folders may hold only
    # pre-release files or no Windows build at all.
    for version in versions:
        file_name = f"python-{version}-embed-amd64.zip"
        directory = f"{_ROOT}{version}/"
        if f'href="{file_name}"' not in _read(directory):
            continue

        current = context.get("current", {})
        if current.get("version") == version and current.get("appReady", True):
            return None
        return {
            "candidateId": f"python:{version}",
            "version": version,
            "url": directory + file_name,
            "fileName": file_name,
        }
    raise RuntimeError("python.org lists no stable Python embeddable amd64 ZIP")
