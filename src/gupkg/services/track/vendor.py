"""Discover a version from a vendor's JSON document or from a response header.

``track_json`` reads a JSON document and selects values with the JSONPath subset of
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

_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")


def track_json(spec: JsonSpec, env: TrackEnvironment) -> TrackResult:
    """Find the version in the JSON at ``check_url``.

    Raises
    ------
    TrackError
        If the document cannot be read, a path is unusable, or no record yields a version.
    """

    def matcher(pattern: re.Pattern[str], texts: Sequence[str]) -> list[bool]:
        """Decide a ``=~`` filter in the worker process; a failure of the worker is a ``TrackError`` (fail closed)."""
        try:
            return env.regex.fullmatches(pattern, texts)
        except PatternError as error:
            raise TrackError(str(error)) from error

    try:
        document = env.http.json(spec.check_url, spec.headers)
        records = spec.records.select(document, matcher)
    except (OSError, JsonPathError) as error:
        raise TrackError(str(error)) from error

    conditions = [parse_condition(text) for text in spec.filters]
    candidates: list[tuple[str, dict[str, str], DiscoveredChecksum | None]] = []
    for record in records:
        # A record that fails any filter condition is skipped entirely.
        if not all(evaluate_condition(condition, record, matcher) for condition in conditions):
            continue
        for value in _texts(spec.version.select_from(record, matcher)):
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
            for name, path in spec.captures.items():
                first = next(iter(_texts(path.select_from(record, matcher))), None)
                if first is not None:
                    captures[name] = first
            if spec.download is not None:
                link = next(iter(_texts(spec.download.select_from(record, matcher))), None)
                if link is not None:
                    captures["download_url"] = absolute_url(link, spec.check_url)
            checksum = None
            if spec.checksum is not None:
                digest = next(iter(_texts(spec.checksum.select_from(record, matcher))), "")
                digest = digest.removeprefix("sha256:")
                if _SHA256.fullmatch(digest):
                    checksum = DiscoveredChecksum(digest.lower(), "vendor-sums")
            candidates.append((version, captures, checksum))
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

    Raises
    ------
    TrackError
        If the request fails, the server reports no date, or the date cannot be read.
    """
    try:
        response = env.http.head(spec.check_url, spec.headers)
    except HttpError as error:
        raise TrackError(str(error)) from error
    if not 200 <= response.status < 300:
        raise TrackError(f"{spec.check_url} answered HTTP {response.status}")
    stamp = response.headers.get("last-modified")
    if not stamp:
        raise TrackError(f"{spec.check_url} reports no Last-Modified date")
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
