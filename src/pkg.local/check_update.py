"""Discover the newest gupkg release listed in the repository's RELEASES.md.

The release registry is the table in ``RELEASES.md`` at the repository root.
Each row names a version, the commit it was built from, and the SHA-256 of the
release ZIP ``gupkg-<version>-win-amd64.zip`` that is attached to the GitHub
release ``v<version>``. The ZIP is self-contained: it carries the interpreter,
the locked dependencies, the native shims and the application.

Usage and API
-------------
The package manager calls ``check_update(context)`` during an update check; the
context's ``fetch_text`` function reads the table through gupkg's machine
boundary, so the check needs no network access of its own. The returned
candidate describes the newest listed release (its asset URL and
digest), or ``None`` when the installed version is current or newer.
``parse_releases(text)`` is the table parser shared with the tests.

Implementation Approach
-----------------------
The table is fetched over HTTPS, parsed row by row, and ordered by numeric
version. The newest row must carry a digest, because the normal download
verification of gupkg compares the downloaded ZIP with it; a row without one is
an error rather than something to skip. Releases of major version 1 or higher
cannot be installed by this updater, so it names the bootstrap command instead.
"""

from __future__ import annotations

import re
from typing import Any


PKG_MODULE_API = 1

_REPOSITORY = "https://github.com/guraltsev/gupkg"
_RELEASES_URL = "https://raw.githubusercontent.com/guraltsev/gupkg/main/RELEASES.md"

# ``| version | commit | date | sha256 | notes |``. Rows written before the
# digest column existed have notes where the digest now is; they parse with no digest.
_ROW = re.compile(
    r"^\|\s*(?P<version>\d+(?:\.\d+)*)\s*\|\s*`?(?P<commit>[0-9a-fA-F]{40})`?\s*\|"
    r"\s*[^|]*\|\s*(?:`?(?P<sha256>[0-9a-fA-F]{64})`?\s*\|)?"
)


def parse_releases(text: str) -> list[dict[str, str | None]]:
    """Return the release rows of a RELEASES.md table, newest version first.

    Parameters
    ----------
    text : str
        Markdown containing a ``| Version | Commit | Date | SHA-256 | Notes |`` table.

    Returns
    -------
    list[dict[str, str | None]]
        Mappings with ``version``, lower-case ``commit`` and lower-case
        ``sha256`` keys; ``sha256`` is ``None`` for a row without a digest.
    """
    rows = []
    for line in text.splitlines():
        match = _ROW.match(line.strip())
        if match is None:
            continue
        digest = match["sha256"]
        rows.append({
            "version": match["version"],
            "commit": match["commit"].lower(),
            "sha256": digest.lower() if digest else None,
        })
    return sorted(rows, key=lambda row: _version_key(row["version"]), reverse=True)


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest listed gupkg release when it is newer than the installed one.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the installed package version and
        ``fetch_text(url, headers=None, *, timeout=60)``, which returns the text
        served at a URL.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata with the release ZIP's URL and ``sha256``, or
        ``None`` when current.

    Raises
    ------
    RuntimeError
        RELEASES.md cannot be read, lists no release, lists the newest release
        without a digest, or the newest release needs the bootstrap script.
    """
    try:
        fetch_text = context.get("fetch_text")
        if fetch_text is None:
            raise RuntimeError("this gupkg is too old for this package; update gupkg")
        text = fetch_text(_RELEASES_URL, timeout=30)
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

    version, commit, digest = newest["version"], newest["commit"], newest["sha256"]

    # A 1.x release has a different package format; this updater never installs
    # it and leaves the active version alone, so the person re-runs the bootstrap.
    if _version_key(version)[0] >= 1:
        raise RuntimeError(
            f"gupkg {version} is a major release that this version cannot install. "
            "Nothing was changed. Install it by running gupkg-bootstrap.cmd "
            f"({_REPOSITORY}) once."
        )

    # Without a digest the download could not be verified, so refuse the row.
    if digest is None:
        raise RuntimeError(f"RELEASES.md lists gupkg {version} without a SHA-256 digest")
    file_name = f"gupkg-{version}-win-amd64.zip"
    return {
        "candidateId": f"gupkg:{version}:{commit}",
        "version": version,
        "commit": commit,
        "url": f"{_REPOSITORY}/releases/download/v{version}/{file_name}",
        "sha256": digest,
        "fileName": file_name,
    }


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components with trailing zeros removed for ordering."""
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)
