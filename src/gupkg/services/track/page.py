"""Discover a version from the text of one page, one feed or a chain of pages.

``track_page`` scans a page with a regular expression whose named groups become the
captures; ``track_rss`` does the same over the links, titles and enclosure addresses of
an RSS or Atom feed; ``track_chain`` runs several such steps where each step's captures
feed the next step's address and pattern. All three choose among several matches with
the provider's ``select`` rule and expand the ``version`` and ``download`` templates.

Usage and API
-------------
``track_page(spec, env)``, ``track_rss(spec, env)`` and ``track_chain(spec, env)`` return
a ``TrackResult`` or raise ``TrackError``; ``gupkg.services.track.run`` dispatches to
them by provider name.

Implementation Approach
-----------------------
Pages are fetched through the shared client (size-capped) and scanned through the
bounded matcher. Feeds are parsed with the standard XML parser only after a document
type declaration is refused, which removes entity-expansion attacks. In a chain, values
substituted into a later pattern are escaped, so a capture can never change the shape of
the pattern.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping

from ...model.manifest import TrackResult
from ...model.patterns import PatternError
from ...model.trackspec import ChainSpec, PageSpec, RssSpec
from ..http import decode_body
from .common import (
    TrackEnvironment,
    TrackError,
    candidates_from_text,
    candidates_from_texts,
    choose,
    compile_runtime,
    expand,
    finish,
    versioned,
)

_MAX_FEED_ELEMENTS = 200_000
_DOCTYPE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_FEED_TAGS = frozenset({"link", "guid", "title", "enclosure", "content", "id"})


def track_page(spec: PageSpec, env: TrackEnvironment) -> TrackResult:
    """Find the version on one page with ``spec.match``.

    Raises
    ------
    TrackError
        If the page cannot be read, nothing matches, or the match is not unique under
        ``select = "unique"``.
    """
    response = _read(env, spec.check_url, spec.headers)
    found = candidates_from_text(env.regex, spec.match, decode_body(response), {"page_url": response.url}, response.url)
    captures, _key = choose(versioned(found, spec.version), spec.select, f"The pattern on {response.url}")
    return finish(captures, spec.version, spec.download, response.url)


def track_rss(spec: RssSpec, env: TrackEnvironment) -> TrackResult:
    """Find the version among the strings of an RSS or Atom feed with ``spec.match``.

    Raises
    ------
    TrackError
        If the feed cannot be read or parsed, has a document type declaration, or
        nothing matches.
    """
    response = _read(env, spec.check_url, spec.headers)
    strings = feed_strings(decode_body(response))
    found = candidates_from_texts(env.regex, spec.match, list(strings), {"page_url": response.url}, response.url)
    captures, _key = choose(versioned(found, spec.version), spec.select, f"The pattern on the feed {response.url}")
    return finish(captures, spec.version, spec.download, response.url)


MAX_EACH_CANDIDATES = 20


def track_chain(spec: ChainSpec, env: TrackEnvironment) -> TrackResult:
    """Run the fetch steps in order, feeding each step's captures to the next.

    A step with ``optional = true`` that cannot be fetched, matches nothing or names a capture that
    an earlier optional step did not produce is skipped (the checksum link of inkscape: without it the
    version is still found and the registry computes the hash). A step with ``select = "each"`` tries
    its matches in page order, at most ``MAX_EACH_CANDIDATES``, against the steps after it and keeps the
    first match for which they all succeed (shutter-encoder: the home page links several operating
    systems and only one link answers with the Windows file name). Every candidate is fetched through
    the shared client, so its retries and deadlines apply. A regular-expression failure (a pattern that
    runs out of time) is never taken for "this candidate does not match": it stops the check.

    Raises
    ------
    TrackError
        If a required step cannot be fetched or matches nothing, or no candidate of an ``each`` step
        gets through the later steps.
    """
    captures, last_url = _run_steps(spec, env, 0, {}, None)
    captures = dict(captures)
    captures["page_url"] = last_url or ""
    return finish(captures, spec.version, spec.download, last_url)


def _run_steps(
    spec: ChainSpec, env: TrackEnvironment, start: int, captures: Mapping[str, str], last_url: str | None
) -> tuple[Mapping[str, str], str | None]:
    """Run the steps from index *start* and return the captures and the last address."""
    for index in range(start, len(spec.steps)):
        step = spec.steps[index]
        position = index + 1
        try:
            outcome = _fetch_step(env, step, position, captures, last_url)
        except TrackError as error:
            if step.optional and not _is_pattern_failure(error):
                continue
            raise
        if outcome is None:
            continue
        found, text_url = outcome
        if step.select == "each":
            return _try_each(spec, env, index, found, text_url)
        captures, last_url = dict(choose(found, step.select, f"Step {position} of the chain ({text_url})")[0]), text_url
    return captures, last_url


def _fetch_step(env, step, position, captures, last_url):
    """Fetch one step and return ``(matches, response address)``, or ``None`` for a step with no ``match``."""
    values = dict(captures)
    url = urllib.parse.urljoin(last_url or "", expand(step.url, values))
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        raise TrackError(f"Step {position} of the chain has the address {url[:100]!r}, which is not http or https")

    # A step reads either the page body or one response header (shutter-encoder
    # takes the version from ``Content-Disposition``).
    if step.header is not None:
        try:
            response = env.http.head(url, step.headers)
        except OSError as error:
            raise TrackError(str(error)) from error
        if not 200 <= response.status < 300:
            raise TrackError(f"Step {position}: {url} answered HTTP {response.status}")
        text = response.headers.get(step.header.lower(), "")
    else:
        response = _read(env, url, step.headers)
        text = decode_body(response)
    if step.match_text is None:
        return [(dict(captures), "")], response.url
    pattern = compile_runtime(step.match_text, values)
    found = candidates_from_text(env.regex, pattern, text, captures, f"step {position} of the chain ({url})")
    if not found:
        raise TrackError(f"Step {position} of the chain ({url}) matched nothing")
    return found, response.url


def _try_each(spec, env, index, found, address):
    """Return the result of the first candidate of step *index* that the later steps accept."""
    failures: list[str] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    tried = 0
    for candidate, _key in found:
        identity = tuple(sorted(candidate.items()))
        if identity in seen:
            continue
        seen.add(identity)
        if tried >= MAX_EACH_CANDIDATES:
            break
        tried += 1
        try:
            return _run_steps(spec, env, index + 1, dict(candidate), address)
        except TrackError as error:
            if _is_pattern_failure(error):
                raise
            failures.append(str(error)[:160])
    detail = "; ".join(failures[:3])
    raise TrackError(
        f"None of the {tried} candidates of step {index + 1} of the chain got through the later steps"
        + (f" ({detail})" if detail else "")
    )


def _is_pattern_failure(error: TrackError) -> bool:
    """Return whether *error* came from a regular-expression failure (a deadline, a crashed worker), which is never skipped."""
    return isinstance(error.__cause__, PatternError)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(env: TrackEnvironment, url: str, headers: Mapping[str, str]):
    """Fetch *url* through the shared client, turning transport failures into ``TrackError``."""
    try:
        return env.http.get(url, headers)
    except OSError as error:
        raise TrackError(str(error)) from error


def feed_strings(text: str) -> list[str]:
    """Return the link, id, title and enclosure strings of an RSS or Atom document.

    Raises
    ------
    TrackError
        If the document has a ``DOCTYPE`` or ``ENTITY`` declaration, is not well-formed
        XML, or has more than 200000 elements.
    """
    if _DOCTYPE.search(text):
        raise TrackError("The feed has a document type declaration, which is refused")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise TrackError(f"The feed is not well-formed XML: {error}") from error
    strings: list[str] = []
    for count, element in enumerate(root.iter()):
        if count > _MAX_FEED_ELEMENTS:
            raise TrackError("The feed has too many elements")
        name = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        if name not in _FEED_TAGS:
            continue
        if element.text and element.text.strip():
            strings.append(element.text.strip())
        for attribute in ("url", "href"):
            if element.attrib.get(attribute):
                strings.append(element.attrib[attribute])
    return strings
