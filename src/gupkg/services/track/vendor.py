"""Discover a version from a vendor's JSON document or from a response header.

``track_json`` fetches a JSON document and selects values with the JSONPath subset of
``gupkg.model.jsonpath``: records (for example one per release), a filter that keeps the
stable ones, the version, an optional download address and checksum. ``track_last_modified``
uses the ``Last-Modified`` date of an unversioned download as its version, for files such
as Sysinternals tools that are republished under one name.

Usage and API
-------------
``track_json(spec, env)`` and ``track_last_modified(spec, env)`` return a ``TrackResult``
or raise ``TrackError``.

Implementation Approach
-----------------------
JSON is parsed by the standard library and navigated by the subset evaluator, never by
``eval``; every value is converted to text and validated as a version before use. The
date provider reads a single ``HEAD`` response and formats the date in UTC with a
whitelisted ``strftime`` pattern.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timezone
from email.utils import parsedate_to_datetime

from ...model.patterns import PatternError
from ...model.jsonpath import JsonPathError, Node, evaluate_condition, parse_condition
from ...model.manifest import DiscoveredChecksum, TrackResult
from ...model.trackspec import JsonSpec, LastModifiedSpec
from ..http import HttpError
from .common import TrackEnvironment, TrackError, absolute_url, candidates_from_text, finish, valid_version
from ...model.version import compare_versions

# A bare 64-digit hexadecimal SHA-256 digest.
_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")


def track_json(spec: JsonSpec, env: TrackEnvironment) -> TrackResult:
    """Find the version in the JSON at ``check_url``.

    Parameters
    ----------
    spec : JsonSpec
        The validated ``json`` provider specification.
    env : TrackEnvironment
        The boundary, shared HTTP client and regex runner.

    Returns
    -------
    TrackResult
        The chosen record's version, captures and any checksum.

    Raises
    ------
    TrackError
        If the document cannot be read, a path is unusable, or no record yields a version.
    """

    # Filters and paths use the worker-backed _matcher so a hostile pattern cannot hang the check.
    def _matcher(pattern: re.Pattern[str], texts: Sequence[str]) -> list[bool]:
        """Decide a ``=~`` filter in the worker process, failing closed (internal to the provider).

        Raises
        ------
        TrackError
            If the worker fails (deadline, crash).
        """
        # Evaluate the whole batch in one worker call; its failure stops the check.
        try:
            return env.regex.fullmatches(pattern, texts)
        except PatternError as error:
            raise TrackError(str(error)) from error

    # Fetch the document and select its records.
    try:
        document = env.http.json(spec.check_url, spec.headers)
        records = spec.records.select(document, _matcher)
    except (OSError, JsonPathError) as error:
        raise TrackError(str(error)) from error

    # Build one candidate per version value of every record that passes the filters.
    conditions = [parse_condition(text) for text in spec.filters]
    candidates: list[tuple[str, dict[str, str], DiscoveredChecksum | None]] = []
    for record in records:
        # A record that fails any filter condition is skipped entirely.
        if not all(evaluate_condition(condition, record, _matcher) for condition in conditions):
            continue
        for value in _texts(spec.version.select_from(record, _matcher)):
            # Start from the page address, then let ``match`` (if any) refine the version and captures.
            captures: dict[str, str] = {"page_url": spec.check_url}
            version = value
            if spec.match is not None:
                matched = candidates_from_text(env.regex, spec.match, value, captures, spec.check_url)
                if not matched:
                    continue
                captures = dict(matched[0][0])
                version = captures.get("version", value)
            else:
                captures["version"] = version
            # Add the extra captures and the download address from the same record.
            for name, path in spec.captures.items():
                first = next(iter(_texts(path.select_from(record, _matcher))), None)
                if first is not None:
                    captures[name] = first
            if spec.download is not None:
                link = next(iter(_texts(spec.download.select_from(record, _matcher))), None)
                if link is not None:
                    captures["download_url"] = absolute_url(link, spec.check_url)
            # A published digest becomes the discovered checksum when it is a valid SHA-256.
            checksum = None
            if spec.checksum is not None:
                digest = next(iter(_texts(spec.checksum.select_from(record, _matcher))), "")
                digest = digest.removeprefix("sha256:")
                if _SHA256.fullmatch(digest):
                    checksum = DiscoveredChecksum(digest.lower(), "vendor-sums")
            candidates.append((version, captures, checksum))
    # An empty candidate list means no record produced a version.
    if not candidates:
        raise TrackError(f"No record in {spec.check_url} yields a version")

    # Pick the newest by the shared version order, or the first in document order.
    if spec.select == "first":
        version, captures, checksum = candidates[0]
    else:
        version, captures, checksum = candidates[0]
        for other in candidates[1:]:
            if compare_versions(other[0], version) > 0:
                version, captures, checksum = other
    valid_version(version)
    captures = {**captures, "version": version}
    return finish(captures, "${version}", None, spec.check_url, checksum)


def track_last_modified(spec: LastModifiedSpec, env: TrackEnvironment) -> TrackResult:
    """Use the ``Last-Modified`` date of ``check_url`` (UTC, formatted by ``spec.format``) as the version.

    Parameters
    ----------
    spec : LastModifiedSpec
        The validated ``last_modified`` provider specification.
    env : TrackEnvironment
        The boundary and shared HTTP client.

    Returns
    -------
    TrackResult
        The formatted date as version, with the header and addresses as captures.

    Raises
    ------
    TrackError
        If the request fails, the server reports no date, or the date cannot be read.
    """
    # Ask for the headers only and insist on a 2xx answer that carries a date.
    try:
        response = env.http.head(spec.check_url, spec.headers)
    except HttpError as error:
        raise TrackError(str(error)) from error
    if not 200 <= response.status < 300:
        raise TrackError(f"{spec.check_url} answered HTTP {response.status}")
    stamp = response.headers.get("last-modified")
    if not stamp:
        raise TrackError(f"{spec.check_url} reports no Last-Modified date")
    # Parse the HTTP date into UTC; the format string turns it into the version.
    try:
        moment = parsedate_to_datetime(stamp).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError) as error:
        raise TrackError(f"{spec.check_url} has an unreadable Last-Modified date {stamp[:60]!r}") from error
    captures = {
        "version": moment.strftime(spec.format), "last_modified": stamp, "download_url": response.url,
        "page_url": response.url,
    }
    return finish(captures, "${version}", None, response.url)


def _texts(nodes: list[Node]) -> list[str]:
    """Return the scalar values of *nodes* as text (booleans and containers are skipped)."""
    found: list[str] = []
    for node in nodes:
        value = node.value
        if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
            continue
        found.append(str(value))
    return found
