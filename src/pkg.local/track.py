"""Find the newest gupkg release listed in the repository's RELEASES.md (the tracker of gupkg's own package).

``gupkg check-updates gupkg`` (also ``gupkg self check-updates``) runs ``track`` from the package's ``_check_updates``
folder. It never installs anything: it only reports the newest listed version, and ``check-updates`` writes the new version
directory for it (decision D11). The populate script of that directory fetches and verifies the release ZIP itself.

Usage and API
-------------
``track(ctx)`` returns a ``TrackResult`` with the newest version and, as captures, the commit and the SHA-256 of its row.
It raises ``HookError`` when the table cannot be read or lists no release, when the newest row has no digest (a release that
could not be verified is refused, not skipped), and when the newest release has a higher major version than the running gupkg
(that needs the bootstrap, not an update, so nothing is changed and the bootstrap is named).

Implementation Approach
-----------------------
The table is read through the shared HTTP client of the context, parsed by ``releases.parse_releases`` and ordered numerically.
Comparing the result with the versions that are already known is the catalog's job, not the tracker's.
"""

from __future__ import annotations

from gupkg._version import __version__
from gupkg.hooklib import HookError, TrackResult, fetch_text
from releases import REPOSITORY, RELEASES_URL, parse_releases, version_key

PKG_MODULE_API = 2


def track(ctx):
    """Return the newest release of gupkg that RELEASES.md lists.

    Parameters
    ----------
    ctx : HookContext
        The tracker's context; its HTTP client reads the table.

    Returns
    -------
    TrackResult
        The newest version, with the ``commit`` and ``sha256`` of its row as captures.

    Raises
    ------
    HookError
        If the table cannot be read, lists no release, lists the newest one without a digest, or the newest one needs
        the bootstrap because its major version is higher than this gupkg's.
    """
    # Read and parse the release table; a failure to read it is an error, never "no new version".
    try:
        text = fetch_text(ctx, RELEASES_URL)
    except OSError as error:
        raise HookError(f"Could not read the gupkg release list: {error}") from error
    releases = parse_releases(text)
    if not releases:
        raise HookError("RELEASES.md lists no gupkg release")
    newest = releases[0]
    version, commit, digest = newest["version"], newest["commit"], newest["sha256"]

    # A release of a higher major version has a different package format: this gupkg never installs it and changes nothing.
    if version_key(version)[0] > version_key(__version__)[0]:
        raise HookError(
            f"gupkg {version} is a major release that this version ({__version__}) cannot install. "
            f"Nothing was changed. Install it by running gupkg-bootstrap.cmd ({REPOSITORY}) once."
        )

    # Without a digest the download could not be verified, so the row is refused.
    if digest is None:
        raise HookError(f"RELEASES.md lists gupkg {version} without a SHA-256 digest")
    return TrackResult(version, captures={"commit": commit, "sha256": digest})
