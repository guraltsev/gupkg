"""Share the version, capture and candidate handling that every provider needs.

A provider finds *candidates*: a version string plus named captures. This module turns a
set of regular-expression matches into candidates, chooses one by the ``select`` rule,
expands the ``version`` and ``download`` templates, and builds the ``TrackResult``.
Choosing the newest of several candidates uses ``gupkg.model.version.compare_versions``,
the same comparator the catalog uses; providers never decide whether a version is newer
than an installed one.

Usage and API
-------------
``candidates_from_text(...)`` makes candidates from matches, ``choose(...)`` selects
one, ``finish(...)`` produces the ``TrackResult``, and ``expand(...)`` expands a
template. ``valid_version(...)``, ``absolute_url(...)`` and ``TrackError`` come from
``gupkg.services.values`` and are re-exported here.

Implementation Approach
-----------------------
Everything read from a response is treated as data: versions must be safe directory
names, URLs must be http or https, and capture values are stripped of markup entities
but never interpreted. Patterns are scanned through ``gupkg.model.patterns`` so size and
match counts are bounded.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path
from typing import Any

from ...model.manifest import DiscoveredChecksum, TrackResult
from ...model.patterns import PatternError, scan
from ...model.version import compare_versions
from ...system.port import System
from ..http import HttpClient
from ..values import TrackError, absolute_url, compile_runtime, expand, valid_version

_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")


@dataclass(frozen=True)
class TrackEnvironment:
    """Everything a provider may use: the boundary, the shared client and two folders.

    Attributes
    ----------
    system : System
        The side-effect boundary (``git`` runs through it).
    http : HttpClient
        The shared HTTP client.
    work_dir : Path or None
        A scratch folder the provider may create files in (the ``git`` provider needs one).
    directory : Path or None
        The tracker directory (``_check_updates``), where a ``module`` provider's script lives.
    package : str
        The package name, for messages and hook contexts.
    extra : mapping
        Free-form values passed on to a ``module`` provider's hook context.
    """

    system: System
    http: HttpClient
    work_dir: Path | None = None
    directory: Path | None = None
    package: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """One version a provider found, with the captures that go with it."""

    version: str
    captures: Mapping[str, str]
    checksum: DiscoveredChecksum | None = None


def candidates_from_text(
    pattern: re.Pattern[str], text: str, base_captures: Mapping[str, str] | None = None
) -> list[tuple[Mapping[str, str], str]]:
    """Return ``(captures, order_key)`` for every match of *pattern* in *text*.

    Captures hold the non-empty named groups, HTML entities decoded. The order key is
    the ``version`` group when there is one, else the first group, else the match text.
    """
    try:
        matches = scan(pattern, text)
    except PatternError as error:
        raise TrackError(str(error)) from error
    found: list[tuple[Mapping[str, str], str]] = []
    for match in matches:
        captures = {**(base_captures or {})}
        for name, value in match.groupdict().items():
            if value is not None:
                captures[name] = html.unescape(value)
        key = captures.get("version")
        if key is None:
            first = next((html.unescape(value) for value in match.groups() if value), None)
            key = first if first is not None else match.group(0)
        found.append((captures, key))
    return found


def versioned(
    found: list[tuple[Mapping[str, str], str]], template: str
) -> list[tuple[Mapping[str, str], str]]:
    """Return *found* with each order key replaced by the candidate's expanded version template.

    A candidate whose template cannot be expanded (it lacks a capture) is dropped, so
    ordering always compares the versions that will really be returned.
    """
    result: list[tuple[Mapping[str, str], str]] = []
    for captures, _key in found:
        try:
            result.append((captures, expand(template, captures)))
        except TrackError:
            continue
    return result


def choose(
    found: list[tuple[Mapping[str, str], str]], select: str, what: str
) -> tuple[Mapping[str, str], str]:
    """Pick one ``(captures, key)`` pair by the ``select`` rule.

    ``max`` is the newest by version order (ties keep the first), ``first`` the first in
    document order, ``unique`` the only distinct key (an error when there are several).

    Raises
    ------
    TrackError
        If *found* is empty, or ``unique`` finds different keys.
    """
    if not found:
        raise TrackError(f"{what} matched nothing")
    if select == "first":
        return found[0]
    if select == "unique":
        keys = {key for _captures, key in found}
        if len(keys) != 1:
            raise TrackError(f"{what} found {len(keys)} different values ({', '.join(sorted(keys)[:4])}); expected exactly one")
        return found[0]
    # One pass keeps the newest; on a tie the earlier candidate stays.
    best = found[0]
    for candidate in found[1:]:
        if compare_versions(candidate[1], best[1]) > 0:
            best = candidate
    return best


def ordered(found: list[tuple[Mapping[str, str], str]], select: str) -> list[tuple[Mapping[str, str], str]]:
    """Return *found* best first: newest first for ``max``, document order for ``first``."""
    if select == "first":
        return list(found)
    indexes = sorted(range(len(found)), key=cmp_to_key(lambda a, b: compare_versions(found[b][1], found[a][1]) or a - b))
    return [found[index] for index in indexes]


def finish(
    captures: Mapping[str, str],
    version_template: str,
    download_template: str | None,
    page_url: str | None,
    checksum: DiscoveredChecksum | None = None,
) -> TrackResult:
    """Build the ``TrackResult`` from the chosen captures and the provider's templates.

    The ``download_url`` capture is the expanded ``download`` template, or an existing
    ``download_url`` capture, resolved against *page_url*. A ``sha256`` capture of 64
    hexadecimal digits becomes a discovered checksum when none was found otherwise.

    Raises
    ------
    TrackError
        If the version or an address is unusable.
    """
    values = dict(captures)
    if page_url is not None:
        values.setdefault("page_url", page_url)
    version = valid_version(expand(version_template, values))
    values["version"] = version
    if download_template is not None:
        values["download_url"] = absolute_url(expand(download_template, values), page_url)
    elif "download_url" in values:
        values["download_url"] = absolute_url(values["download_url"], page_url)
    if checksum is None and _SHA256.fullmatch(values.get("sha256", "")):
        checksum = DiscoveredChecksum(values["sha256"].lower(), "vendor-sums")
    return TrackResult(version, values, checksum)
