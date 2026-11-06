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
Everything taken from a response is treated as data: versions must be safe directory
names, URLs must be http or https, and capture values are stripped of markup entities
but never interpreted. Patterns are applied through ``gupkg.services.regexrun``, in a worker
process with a deadline, so a hostile page cannot hang the run.
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
from ...model.patterns import PatternError
from ...model.version import compare_versions
from ...model.scope import Scope
from ...system.port import System
from ..http import HttpClient
from ..regexrun import RegexRunner
from ..values import TrackError, absolute_url, compile_runtime, expand, valid_version

# A bare 64-digit hexadecimal SHA-256 digest.
_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")


@dataclass(frozen=True)
class TrackEnvironment:
    """Everything a provider may use: the boundary, the shared client and two folders.

    Attributes
    ----------
    system : System
        The side-effect boundary (``git`` runs through it).
    scope : Scope
        The run's scope: in system mode only a trusted ``git`` and trusted hooks run.
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
    regex : RegexRunner, optional
        Runs the manifest patterns in a worker process with a deadline; one is created for the
        environment when omitted, so its total time budget belongs to this provider run.
    """

    system: System
    scope: Scope
    http: HttpClient
    work_dir: Path | None = None
    directory: Path | None = None
    package: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)
    regex: RegexRunner | None = None

    def __post_init__(self) -> None:
        """Give the provider run its own regex runner (and so its own total time budget) unless one was passed."""
        if self.regex is None:
            object.__setattr__(self, "regex", RegexRunner(self.system))


@dataclass(frozen=True)
class Candidate:
    """One version a provider found, with the captures that go with it.

    Attributes
    ----------
    version : str
        The version the provider found.
    captures : mapping of str to str
        The named values found with the version.
    checksum : DiscoveredChecksum or None
        The digest the provider found, when it found one.
    """

    version: str
    captures: Mapping[str, str]
    checksum: DiscoveredChecksum | None = None


def candidates_from_text(
    regex: RegexRunner,
    pattern: re.Pattern[str],
    text: str,
    base_captures: Mapping[str, str] | None = None,
    what: str = "",
) -> list[tuple[Mapping[str, str], str]]:
    """Return ``(captures, order_key)`` for every match of *pattern* in *text*.

    The match runs in a worker process with a deadline (*regex*), because *text* comes from the
    network. Captures hold the non-empty named groups, HTML entities decoded. The order key is
    the ``version`` group when there is one, else the first group, else the match text. *what*
    names the field or address for an error message.

    Parameters
    ----------
    regex : RegexRunner
        Runs the pattern in a worker process with a deadline.
    pattern : re.Pattern
        The compiled pattern to match.
    text : str
        The text to search.
    base_captures : mapping, optional
        Captures every candidate starts from.
    what : str
        Field or address named in error messages.

    Returns
    -------
    list of tuple
        One ``(captures, order_key)`` pair per match.

    Raises
    ------
    TrackError
        If the pattern fails (deadline, crashed worker).
    """
    return candidates_from_texts(regex, pattern, [text], base_captures, what)


def candidates_from_texts(
    regex: RegexRunner,
    pattern: re.Pattern[str],
    texts: list[str],
    base_captures: Mapping[str, str] | None = None,
    what: str = "",
) -> list[tuple[Mapping[str, str], str]]:
    """Return ``(captures, order_key)`` for every match of *pattern* in each of *texts*, in order.

    Parameters
    ----------
    regex : RegexRunner
        Runs the pattern in one worker process that serves all texts.
    pattern : re.Pattern
        The compiled pattern to match.
    texts : list of str
        The texts to search, in order.
    base_captures : mapping, optional
        Captures every candidate starts from.
    what : str
        Field or address named in error messages.

    Returns
    -------
    list of tuple
        One ``(captures, order_key)`` pair per match.

    Raises
    ------
    TrackError
        If the pattern fails (deadline, crashed worker).
    """
    # Run the pattern over every text in a single worker; pattern failures become tracker errors.
    try:
        matches = [match for one in regex.finditer_many(pattern, texts, what=what) for match in one]
    except PatternError as error:
        raise TrackError(str(error)) from error
    # Build one candidate per match: named groups become captures; the order key is the version group,
    # else the first non-empty group, else the whole match.
    found: list[tuple[Mapping[str, str], str]] = []
    for match in matches:
        captures = {**(base_captures or {})}
        for name, value in match.groupdict().items():
            if value is not None:
                captures[name] = html.unescape(value)
        # The order key falls back from the version group to the first non-empty group to the match.
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

    Parameters
    ----------
    found : list of tuple
        ``(captures, key)`` pairs from ``candidates_from_text``.
    template : str
        The ``version`` template to expand against each candidate's captures.

    Returns
    -------
    list of tuple
        The surviving pairs, each keyed by its expanded version.
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

    Parameters
    ----------
    found : list of tuple
        ``(captures, key)`` pairs to choose from.
    select : str
        The rule: ``max``, ``first`` or ``unique``.
    what : str
        Field or address named in error messages.

    Returns
    -------
    tuple
        The chosen ``(captures, key)`` pair.

    Raises
    ------
    TrackError
        If *found* is empty, or ``unique`` finds different keys.

    Examples
    --------
    Basic usage:

    >>> found = [({'v': '1.9'}, '1.9'), ({'v': '1.10'}, '1.10')]
    >>> choose(found, 'max', 'page')
    ({'v': '1.10'}, '1.10')
    >>> choose(found, 'first', 'page')
    ({'v': '1.9'}, '1.9')

    Edge cases:

    >>> choose([], 'max', 'the page')
    Traceback (most recent call last):
        ...
    gupkg.services.values.TrackError: the page matched nothing
    >>> choose(found, 'unique', 'the page')
    Traceback (most recent call last):
        ...
    gupkg.services.values.TrackError: the page found 2 different values (1.10, 1.9); expected exactly one
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
    """Return *found* best first: newest first for ``max``, document order for ``first``.

    Parameters
    ----------
    found : list of tuple
        ``(captures, key)`` pairs.
    select : str
        The rule: ``first`` keeps document order, anything else orders newest first.

    Returns
    -------
    list of tuple
        A new list with the best candidate first; ties keep document order.

    Examples
    --------
    Basic usage:

    >>> found = [({}, '1.9'), ({}, '1.10'), ({}, '1.2')]
    >>> [key for _captures, key in ordered(found, 'max')]
    ['1.10', '1.9', '1.2']
    >>> [key for _captures, key in ordered(found, 'first')]
    ['1.9', '1.10', '1.2']
    """
    # Document order needs no sorting; otherwise sort newest first with the original index breaking ties.
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

    Parameters
    ----------
    captures : mapping
        The chosen captures.
    version_template : str
        Template for the version.
    download_template : str or None
        Template for the download address, if the provider declares one.
    page_url : str or None
        Address relative URLs are resolved against.
    checksum : DiscoveredChecksum, optional
        A checksum the provider already found.

    Returns
    -------
    TrackResult
        The validated version, the expanded captures and any checksum.

    Raises
    ------
    TrackError
        If the version or an address is unusable.
    """
    # Expand the version first (it feeds the download template), then resolve the download address.
    values = dict(captures)
    if page_url is not None:
        values.setdefault("page_url", page_url)
    version = valid_version(expand(version_template, values))
    values["version"] = version
    if download_template is not None:
        values["download_url"] = absolute_url(expand(download_template, values), page_url)
    elif "download_url" in values:
        values["download_url"] = absolute_url(values["download_url"], page_url)
    # A vendor-supplied sha256 capture serves as the checksum when the provider found none.
    if checksum is None and _SHA256.fullmatch(values.get("sha256", "")):
        checksum = DiscoveredChecksum(values["sha256"].lower(), "vendor-sums")
    return TrackResult(version, values, checksum)
