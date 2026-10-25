"""Read the release table of gupkg's repository: what the tracker and the populate script both need.

The release registry is the table in ``RELEASES.md`` at the repository root. Each row names a version, the commit it was
built from, and the SHA-256 of the release ZIP ``gupkg-<version>-win-amd64.zip`` that is attached to the GitHub release
``v<version>``. The ZIP is a package-root fragment of the gupkg package: ``gupkg\\<version>\\`` with the bundled tree
``gupkg\\`` inside it, and ``gupkg\\_check_updates\\``.

Usage and API
-------------
``parse_releases(text)`` returns the rows, newest version first, as dictionaries with ``version``, lower-case ``commit`` and
lower-case ``sha256`` (``None`` for a row without a digest). ``asset_url(version)`` is where the release ZIP of a version is
published. ``version_key(version)`` orders versions numerically. ``RELEASES_URL`` and ``REPOSITORY`` name the sources.

Implementation Approach
-----------------------
Rows are matched line by line with one expression, so a row that does not parse is ignored rather than guessed at. Ordering is
numeric with trailing zeros removed, so ``1.0`` and ``1.0.0`` are one version. The module has no ``track`` function, so
``check-updates`` copies it into every new version directory, where the populate script imports it.
"""

from __future__ import annotations

import re

REPOSITORY = "https://github.com/guraltsev/gupkg"
RELEASES_URL = "https://raw.githubusercontent.com/guraltsev/gupkg/main/RELEASES.md"

# ``| version | commit | date | sha256 | notes |``. Rows written before the digest column existed have notes
# where the digest now is; they parse with no digest.
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
        Mappings with ``version``, lower-case ``commit`` and lower-case ``sha256`` keys; ``sha256`` is ``None`` for a
        row without a digest.
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
    return sorted(rows, key=lambda row: version_key(row["version"]), reverse=True)


def asset_url(version: str) -> str:
    """Return the address of the release ZIP of *version* (an asset of the GitHub release ``v<version>``)."""
    return f"{REPOSITORY}/releases/download/v{version}/gupkg-{version}-win-amd64.zip"


def version_key(version: str) -> tuple[int, ...]:
    """Return the numeric components of *version* with trailing zeros removed, for ordering."""
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)
