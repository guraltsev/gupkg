"""Parse the release table of gupkg's repository: what the tracker and the populate script both need.

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

# Home of the gupkg repository, from which release assets are downloaded.
REPOSITORY = "https://github.com/guraltsev/gupkg"
# Raw address of the release table.
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

    Examples
    --------
    Basic usage:

    >>> table = '| Version | Commit | Date | SHA-256 | Notes |\\n|---|---|---|---|---|\\n| 1.0.9 | ' + 'A' * 40 + ' | 2026-01-01 | ' + 'b' * 64 + ' | first |\\n| 1.0.10 | ' + 'c' * 40 + ' | 2026-02-01 | - | second |\\n'
    >>> [(row['version'], row['commit'][:7], row['sha256'] and row['sha256'][:7]) for row in parse_releases(table)]
    [('1.0.10', 'ccccccc', None), ('1.0.9', 'aaaaaaa', 'bbbbbbb')]

    Edge cases:

    >>> parse_releases('no table here')
    []
    """
    # Keep only lines that match a release row; anything else is ignored rather than guessed at.
    rows = []
    for line in text.splitlines():
        match = _ROW.match(line.strip())
        if match is None:
            continue
        # Normalise hex digits to lower case; a row without a digest keeps ``None``.
        digest = match["sha256"]
        rows.append({
            "version": match["version"],
            "commit": match["commit"].lower(),
            "sha256": digest.lower() if digest else None,
        })
    # Newest version first, by numeric order.
    return sorted(rows, key=lambda row: version_key(row["version"]), reverse=True)


def asset_url(version: str) -> str:
    """Return the address of the release ZIP of *version* (an asset of the GitHub release ``v<version>``).

    Parameters
    ----------
    version : str
        Dotted version, such as ``1.1.14``.

    Returns
    -------
    str
        The download address of ``gupkg-<version>-win-amd64.zip``.

    Examples
    --------
    Basic usage:

    >>> asset_url('1.1.14').rsplit('/', 3)[1:]
    ['download', 'v1.1.14', 'gupkg-1.1.14-win-amd64.zip']
    """
    return f"{REPOSITORY}/releases/download/v{version}/gupkg-{version}-win-amd64.zip"


def version_key(version: str) -> tuple[int, ...]:
    """Return the numeric components of *version* with trailing zeros removed, for ordering.

    Parameters
    ----------
    version : str
        Dotted numeric version.

    Returns
    -------
    tuple of int
        Components usable as a sort key; ``1.0`` and ``1.0.0`` give the same key.

    Examples
    --------
    Basic usage:

    >>> version_key('1.1.14')
    (1, 1, 14)
    >>> version_key('1.0') == version_key('1.0.0')
    True

    Edge cases:

    >>> sorted(['1.0.9', '1.0.10', '1.0.2'], key=version_key)
    ['1.0.2', '1.0.9', '1.0.10']
    """
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)
