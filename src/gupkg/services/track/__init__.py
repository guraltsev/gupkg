"""Run a tracker's ``[track]`` provider and discover the checksum of what it found.

``run_tracker(manifest, env)`` is the one call that ``gupkg check-updates`` makes for step
one of design section 7.4: it validates the tracker's ``[track]`` table, runs the named
provider (``github``, ``gitlab``, ``page``, ``index``, ``redirect``, ``json``,
``last_modified``, ``rss``, ``git``, ``chain`` or ``module``) and, when the tracker declares
``[source.checksum]``, finds the digest for the version that was discovered. The result is
a ``TrackResult`` ready for ``gupkg.model.manifest.render_pinned``.

Usage and API
-------------
Build a ``TrackEnvironment`` (the ``System``, a shared ``HttpClient``, a scratch folder and
the tracker directory) and call ``run_tracker(manifest, env)``. ``run_provider(spec, env)``
runs a validated specification directly. Providers never compare versions with what is
installed; that is the catalog's job.

Implementation Approach
-----------------------
Providers are plain functions keyed by their specification type, so a manifest that
validates always has a runner. Provider captures and the discovered checksum travel in the
``TrackResult``; an explicit ``[source.checksum]`` replaces a digest the provider found on
its own.
"""

from __future__ import annotations

from dataclasses import replace

from ...model.manifest import Manifest, ManifestKind, TrackResult
from ...model.patterns import PatternError
from ...model.template import render
from ...model.trackspec import (
    ChainSpec,
    GithubSpec,
    GitlabSpec,
    GitSpec,
    IndexSpec,
    JsonSpec,
    LastModifiedSpec,
    ModuleSpec,
    PageSpec,
    RedirectSpec,
    RssSpec,
    TrackProviderSpec,
    TrackSpecError,
    parse_spec,
)
from ..checksum import ChecksumDiscoveryError, discover_checksum
from .common import TrackEnvironment, TrackError
from .index import track_index, track_redirect
from .page import track_chain, track_page, track_rss
from .releases import track_github, track_gitlab
from .repository import track_git, track_module
from .vendor import track_json, track_last_modified

_RUNNERS = {
    GithubSpec: track_github, GitlabSpec: track_gitlab, PageSpec: track_page, IndexSpec: track_index,
    RedirectSpec: track_redirect, JsonSpec: track_json, LastModifiedSpec: track_last_modified,
    RssSpec: track_rss, GitSpec: track_git, ChainSpec: track_chain, ModuleSpec: track_module,
}

__all__ = ["ChecksumDiscoveryError", "TrackEnvironment", "TrackError", "run_provider", "run_tracker"]


def run_provider(spec: TrackProviderSpec, env: TrackEnvironment) -> TrackResult:
    """Run the provider that *spec* describes and return what it discovered."""
    return _RUNNERS[type(spec)](spec, env)


def run_tracker(manifest: Manifest, env: TrackEnvironment) -> TrackResult:
    """Discover the newest version of a tracker, with its captures and any discovered checksum.

    Parameters
    ----------
    manifest : Manifest
        The parsed manifest of ``_check_updates`` (kind ``TRACKER``, with ``[track]``).
    env : TrackEnvironment
        The boundary, shared HTTP client and folders the providers use.

    Returns
    -------
    TrackResult
        The version, the captures, and a checksum from the provider (a GitHub digest) or
        from ``[source.checksum]``.

    Raises
    ------
    TrackError
        If the manifest is not a tracker, ``[track]`` is invalid, or the provider fails.
    ChecksumDiscoveryError
        If ``[source.checksum]`` is declared (and not ``optional = true``) but its source has no
        digest for the version.
    """
    track = manifest.track
    if manifest.kind is not ManifestKind.TRACKER or track is None:
        raise TrackError("Only a tracker (_check_updates with a [track] table) can be run")
    location = {"check_url": track.check_url, "repo": track.repo, "repo_url": track.repo_url}
    try:
        spec = parse_spec(track.provider, location, track.options)
    except TrackSpecError as error:
        raise TrackError(f"[track] is invalid: {error}") from error
    env = replace(env, extra={**env.extra, **track.options})
    result = run_provider(spec, env)

    # An explicit ``[source.checksum]`` finds the digest of the version that was discovered.
    declared = manifest.source.checksum
    if declared is None:
        return result
    values = dict(result.captures)
    values["version"] = result.version
    if manifest.source.download_url is not None:
        values["download_url"] = render(manifest.source.download_url, values)
    try:
        checksum = discover_checksum(env.http, declared, values, env.regex)
    except ChecksumDiscoveryError as error:
        # An optional source (the vendor publishes a digest for some builds only) falls back to no
        # discovered checksum, never past a regular-expression failure, which stops the check.
        if declared.optional and not _pattern_failure(error):
            return result
        raise
    return replace(result, checksum=checksum)


def _pattern_failure(error: BaseException) -> bool:
    """Return whether a regular-expression failure (deadline, crashed worker) is anywhere in *error*'s cause chain."""
    seen = 0
    while error is not None and seen < 10:
        if isinstance(error, PatternError):
            return True
        error, seen = error.__cause__, seen + 1
    return False
