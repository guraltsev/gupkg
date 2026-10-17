"""Discover the newest gupkg release listed in the repository's RELEASES.md.

The release registry is the table in ``RELEASES.md`` at the repository root.
Each row names a version and the full commit hash whose source archive is the
release. The updater needs no hosted build artifact: GitHub serves a source
archive for any commit.

Usage and API
-------------
The package manager calls ``check_update(context)`` during an update check. The
returned candidate describes the newest listed release, or ``None`` when the
installed version is current or newer. ``parse_releases(text)`` is the table
parser shared with the tests.

Implementation Approach
-----------------------
The table is fetched over HTTPS, parsed row by row, and ordered by numeric
version. The candidate URL is the commit's archive; ``unpack_app`` checks that
the archive was built from that commit before using it.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_REPOSITORY = "https://github.com/guraltsev/gupkg"
_RELEASES_URL = "https://raw.githubusercontent.com/guraltsev/gupkg/main/RELEASES.md"
_ROW = re.compile(
    r"^\|\s*(?P<version>\d+(?:\.\d+)*)\s*\|\s*`?(?P<commit>[0-9a-fA-F]{40})`?\s*\|"
)


def parse_releases(text: str) -> list[dict[str, str]]:
    """Return the release rows of a RELEASES.md table, newest version first.

    Parameters
    ----------
    text : str
        Markdown containing a ``| Version | Commit | ... |`` table.

    Returns
    -------
    list[dict[str, str]]
        Mappings with ``version`` and lower-case ``commit`` keys.
    """
    rows = [
        {"version": match["version"], "commit": match["commit"].lower()}
        for line in text.splitlines()
        if (match := _ROW.match(line.strip())) is not None
    ]
    return sorted(rows, key=lambda row: _version_key(row["version"]), reverse=True)


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest listed gupkg release when it is newer than the installed one.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the installed package version.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata for the commit archive, or ``None`` when current.

    Raises
    ------
    RuntimeError
        RELEASES.md cannot be read or lists no release.
    """
    try:
        with urllib.request.urlopen(_RELEASES_URL, timeout=30) as response:
            text = response.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read the gupkg release list: {exc}") from exc
    releases = parse_releases(text)
    if not releases:
        raise RuntimeError("RELEASES.md lists no gupkg release")
    newest = releases[0]

    installed = context.get("current", {}).get("version")
    if (
        isinstance(installed, str)
        and re.fullmatch(r"\d+(?:\.\d+)*", installed)
        and _version_key(newest["version"]) <= _version_key(installed)
    ):
        return None

    version, commit = newest["version"], newest["commit"]
    return {
        "candidateId": f"gupkg:{version}:{commit}",
        "version": version,
        "commit": commit,
        "url": f"{_REPOSITORY}/archive/{commit}.zip",
        "fileName": f"gupkg-{version}.zip",
    }


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components with trailing zeros removed for ordering."""
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)
