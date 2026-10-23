"""Discover a version from the releases of a GitHub repository or a GitLab project.

Both providers read a release list through the shared client, normalise it to one shape
(a tag, assets with a name, an address and optionally a SHA-256, a prerelease flag),
derive the version from the tag or from an asset name, require the asset the package
needs, and return the newest matching release. A digest the host publishes for the asset
becomes the result's discovered checksum, so no hash has to be scraped.

Usage and API
-------------
``track_github(spec, env)`` and ``track_gitlab(spec, env)`` return a ``TrackResult`` or
raise ``TrackError``. The captures are ``tag``, ``asset_name``, ``asset_url``,
``release_url`` and the named groups of ``tag_pattern`` and ``asset_pattern``.

Implementation Approach
-----------------------
``release = "latest"`` asks GitHub for its single latest release; ``newest-matching``
reads up to a hundred releases and keeps the newest one that has a usable version and,
when an asset is requested, exactly one matching asset. Hosts return JSON that is treated
as data: every field is type-checked before use, and asset addresses must be https.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ...model.manifest import DiscoveredChecksum, TrackResult
from ...model.patterns import PatternError
from ...model.trackspec import GithubSpec, GitlabSpec
from ...model.version import compare_versions
from ..http import HttpError
from ..regexrun import RegexRunner
from .common import (
    TrackEnvironment,
    TrackError,
    absolute_url,
    compile_runtime,
    expand,
    finish,
    valid_version,
)

GITHUB_API = "https://api.github.com"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_DIGEST = re.compile(r"sha256:([0-9A-Fa-f]{64})")
_DEFAULT_TAG = re.compile(r"[vV]?(?P<version>.+)")


@dataclass(frozen=True)
class Asset:
    """One downloadable file of a release."""

    name: str
    url: str
    sha256: str | None = None


@dataclass(frozen=True)
class Release:
    """One release in the shape both hosts are normalised to."""

    tag: str
    assets: tuple[Asset, ...]
    url: str = ""
    prerelease: bool = False


@dataclass(frozen=True)
class _Options:
    """The selection rules shared by the two providers."""

    asset: str | None
    asset_pattern: str | None
    version_from: str
    tag_pattern: re.Pattern[str] | None
    tag_prefix: str | None
    version: str
    link_name: str | None = None
    prerelease: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)
    regex: "_Matches | None" = None


def track_github(spec: GithubSpec, env: TrackEnvironment) -> TrackResult:
    """Find the newest matching GitHub release of ``spec.repo``.

    Raises
    ------
    TrackError
        If the API cannot be read, or no release has a usable version and the requested asset.
    """
    base = f"{GITHUB_API}/repos/{spec.repo}/releases"
    try:
        if spec.release == "latest":
            data = env.http.json(f"{base}/latest", _GITHUB_HEADERS)
            rows = [data] if isinstance(data, dict) else []
        else:
            data = env.http.json(f"{base}?per_page=100", _GITHUB_HEADERS)
            rows = data if isinstance(data, list) else []
    except HttpError as error:
        raise TrackError(f"Could not read the releases of {spec.repo}: {error}") from error
    releases = [release for release in (_github_release(row) for row in rows) if release is not None]
    options = _Options(spec.asset, spec.asset_pattern, spec.version_from, spec.tag_pattern, spec.tag_prefix,
                       spec.version, prerelease=spec.prerelease, regex=_Matches(env.regex))
    return _checked_best(releases, options, f"GitHub repository {spec.repo}", strict=spec.release == "latest")


def track_gitlab(spec: GitlabSpec, env: TrackEnvironment) -> TrackResult:
    """Find the newest matching GitLab release of ``spec.project``.

    Raises
    ------
    TrackError
        If the API cannot be read, or no release has a usable version and the requested link.
    """
    project = urllib.parse.quote(spec.project, safe="")
    try:
        data = env.http.json(f"{spec.base}/api/v4/projects/{project}/releases?per_page=100")
    except HttpError as error:
        raise TrackError(f"Could not read the releases of {spec.project}: {error}") from error
    rows = data if isinstance(data, list) else []
    releases = [release for release in (_gitlab_release(row) for row in rows) if release is not None]
    options = _Options(None, spec.asset_pattern, "tag", spec.tag_pattern, spec.tag_prefix, spec.version,
                       link_name=spec.link_name, regex=_Matches(env.regex))
    return _checked_best(releases, options, f"GitLab project {spec.project}", strict=False)


# ---------------------------------------------------------------------------
# Normalising the hosts' JSON
# ---------------------------------------------------------------------------


def _github_release(row: Any) -> Release | None:
    """Return a ``Release`` from one GitHub release object, or ``None`` for a draft or a malformed row."""
    if not isinstance(row, dict) or row.get("draft") is True or not isinstance(row.get("tag_name"), str):
        return None
    assets: list[Asset] = []
    for item in row.get("assets") if isinstance(row.get("assets"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        if item.get("state", "uploaded") != "uploaded" or not isinstance(item.get("browser_download_url"), str):
            continue
        digest = _DIGEST.fullmatch(item["digest"]) if isinstance(item.get("digest"), str) else None
        assets.append(Asset(item["name"], item["browser_download_url"], digest.group(1).lower() if digest else None))
    page = row.get("html_url") if isinstance(row.get("html_url"), str) else ""
    return Release(row["tag_name"], tuple(assets), page, row.get("prerelease") is True)


def _gitlab_release(row: Any) -> Release | None:
    """Return a ``Release`` from one GitLab release object, or ``None`` when malformed."""
    if not isinstance(row, dict) or not isinstance(row.get("tag_name"), str):
        return None
    links = (row.get("assets") or {}).get("links") if isinstance(row.get("assets"), dict) else None
    assets: list[Asset] = []
    for item in links if isinstance(links, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        address = item.get("direct_asset_url") or item.get("url")
        if isinstance(address, str):
            digest = next((str(item[key]) for key in ("sha256", "digest") if item.get(key)), "")
            found = _DIGEST.fullmatch(digest) or re.fullmatch(r"([0-9A-Fa-f]{64})", digest)
            assets.append(Asset(item["name"], address, found.group(1).lower() if found else None))
    page = (row.get("_links") or {}).get("self") if isinstance(row.get("_links"), dict) else ""
    return Release(row["tag_name"], tuple(assets), page if isinstance(page, str) else "", False)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class _Matches:
    """Match patterns against tags and asset names through the regex worker, asking for each (pattern, text) once.

    A run over many releases would otherwise start a worker per release and per asset list. ``many``
    matches all the texts it has not seen in one call, and ``_prime`` asks for all tags and all asset
    names up front (when the pattern does not depend on the release), so a run needs two calls instead of
    two per release. Results are the worker's, unchanged.
    """

    def __init__(self, runner: RegexRunner) -> None:
        """Remember the runner."""
        self._runner = runner
        self._known: dict[tuple[str, int, str], Any] = {}

    def many(self, pattern: re.Pattern[str], texts: list[str], what: str) -> list[Any]:
        """Return, for every text, the match of *pattern* against the whole of it or ``None``."""
        missing = [text for text in dict.fromkeys(texts) if (pattern.pattern, pattern.flags, text) not in self._known]
        if missing:
            for text, found in zip(missing, self._runner.fullmatch_many(pattern, missing, what=what)):
                self._known[(pattern.pattern, pattern.flags, text)] = found
        return [self._known[(pattern.pattern, pattern.flags, text)] for text in texts]

    def one(self, pattern: re.Pattern[str], text: str, what: str) -> Any:
        """Return the match of *pattern* against the whole of *text*, or ``None``."""
        return self.many(pattern, [text], what)[0]


def _prime(options: _Options, releases: list[Release]) -> None:
    """Match every tag, and every asset name when the asset pattern is the same for all releases, in one call each."""
    considered = [release for release in releases if options.prerelease or not release.prerelease]
    if options.tag_pattern is not None and considered:
        options.regex.many(options.tag_pattern, [release.tag for release in considered], "the release tags")
    uses_pattern = options.asset_pattern is not None and (
        options.version_from == "asset" or (options.asset is None and options.link_name is None)
    )
    if uses_pattern and "${" not in (options.asset_pattern or ""):
        names = [asset.name for release in considered for asset in release.assets]
        if names:
            options.regex.many(compile_runtime(options.asset_pattern or "", {}), names, "the asset names of the releases")


def _checked_best(releases: list[Release], options: _Options, where: str, *, strict: bool) -> TrackResult:
    """Run ``_best``; a failure of the regex worker stops the whole provider (fail closed), it never skips a release."""
    try:
        _prime(options, releases)
        return _best(releases, options, where, strict=strict)
    except PatternError as error:
        raise TrackError(str(error)) from error


def _best(releases: list[Release], options: _Options, where: str, *, strict: bool) -> TrackResult:
    """Return the result for the newest release that satisfies *options*.

    With *strict* a release that does not satisfy them is an error naming the reason
    (the single "latest" release must be usable); otherwise it is skipped.
    """
    results: list[tuple[str, TrackResult]] = []
    reasons: list[str] = []
    for release in releases:
        if release.prerelease and not options.prerelease:
            reasons.append(f"{release.tag}: prerelease")
            continue
        try:
            result = _from_release(release, options)
        except TrackError as error:
            reasons.append(f"{release.tag}: {error}")
            continue
        results.append((result.version, result))
    if not results:
        detail = f" ({'; '.join(reasons[:3])})" if reasons else ""
        raise TrackError(f"{where} has no release with a usable version{' and the requested asset' if options.asset or options.asset_pattern or options.link_name else ''}{detail}")
    if strict and len(results) != 1:
        raise TrackError(f"{where} has more than one latest release")
    best = results[0]
    for other in results[1:]:
        if compare_versions(other[0], best[0]) > 0:
            best = other
    return best[1]


def _from_release(release: Release, options: _Options) -> TrackResult:
    """Derive the version, pick the asset and build the result for one release."""
    captures: dict[str, str] = {"tag": release.tag, "release_url": release.url}
    groups = _tag_groups(release.tag, options)
    captures.update(groups)
    asset: Asset | None = None

    # Choose the asset first when the version comes from its name.
    if options.version_from == "asset":
        pattern = compile_runtime(options.asset_pattern or "", {**captures, "version": groups.get("version", "")})
        verdicts = options.regex.many(pattern, [item.name for item in release.assets], f"the assets of release {release.tag}")
        found = list(zip(release.assets, verdicts))
        found = [(item, match) for item, match in found if match]
        if len(found) != 1:
            raise TrackError(f"expected one asset matching {options.asset_pattern!r}, found {len(found)}")
        asset, match = found[0]
        captures.update({key: value for key, value in match.groupdict().items() if value is not None})
    version_text = expand(options.version, {**captures, "version": captures.get("version", groups.get("version", ""))})
    version = valid_version(version_text)
    captures["version"] = version

    # With the version known, find the asset by exact name, pattern, or GitLab link name.
    if asset is None and (options.asset or options.asset_pattern or options.link_name):
        if options.asset is not None or options.link_name is not None:
            wanted = expand(options.asset or options.link_name or "", captures)
            found = [item for item in release.assets if item.name == wanted]
        else:
            pattern = compile_runtime(options.asset_pattern or "", captures)
            verdicts = options.regex.many(pattern, [item.name for item in release.assets], f"the assets of release {release.tag}")
            matches = list(zip(release.assets, verdicts))
            found = [item for item, match in matches if match]
            for _item, match in matches:
                if match:
                    captures.update({key: value for key, value in match.groupdict().items() if value is not None and key != "version"})
        if len(found) != 1:
            raise TrackError(f"expected exactly one matching asset, found {len(found)}")
        asset = found[0]

    checksum = None
    if asset is not None:
        captures["asset_name"] = asset.name
        captures["asset_url"] = _asset_address(asset.url)
        if asset.sha256 is not None:
            checksum = DiscoveredChecksum(asset.sha256, "github-digest")
    return finish(captures, "${version}", None, release.url or None, checksum)


def _tag_groups(tag: str, options: _Options) -> dict[str, str]:
    """Return the named values derived from a tag: ``version`` and any pattern groups.

    Raises
    ------
    TrackError
        If the tag does not fit ``tag_prefix`` or ``tag_pattern``.
    """
    if options.tag_pattern is not None:
        match = options.regex.one(options.tag_pattern, tag, f"the tag {tag[:60]!r}")
        if match is None:
            raise TrackError(f"the tag {tag!r} does not match tag_pattern")
        return {key: value for key, value in match.groupdict().items() if value is not None}
    if options.tag_prefix is not None:
        if not tag.startswith(options.tag_prefix):
            raise TrackError(f"the tag {tag!r} does not start with {options.tag_prefix!r}")
        return {"version": tag[len(options.tag_prefix):]}
    if options.version_from == "asset":
        return {}
    match = _DEFAULT_TAG.fullmatch(tag)
    return {"version": match.group("version")} if match else {}


def _asset_address(url: str) -> str:
    """Return an asset address after checking that it is a https URL."""
    address = absolute_url(url)
    if not address.lower().startswith("https://"):
        raise TrackError(f"the asset address {address[:100]!r} is not https")
    return address
