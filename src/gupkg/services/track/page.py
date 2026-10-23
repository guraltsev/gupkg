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


def track_chain(spec: ChainSpec, env: TrackEnvironment) -> TrackResult:
    """Run the fetch steps in order, feeding each step's captures to the next.

    Raises
    ------
    TrackError
        If a step cannot be fetched or matches nothing.
    """
    captures: dict[str, str] = {}
    last_url: str | None = None
    for position, step in enumerate(spec.steps, start=1):
        values = dict(captures)
        url = urllib.parse.urljoin(last_url or "", expand(step.url, values))
        if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
            raise TrackError(f"Step {position} of the chain has the address {url[:100]!r}, which is not http or https")

        # A step reads either the page body or one response header (shutter-encoder
        # takes the version from ``Content-Disposition``).
        if step.header is not None:
            response = env.http.head(url, step.headers)
            if not 200 <= response.status < 300:
                raise TrackError(f"Step {position}: {url} answered HTTP {response.status}")
            text = response.headers.get(step.header.lower(), "")
        else:
            response = _read(env, url, step.headers)
            text = decode_body(response)
        if step.match_text is None:
            last_url = response.url
            continue
        pattern = compile_runtime(step.match_text, values)
        found = candidates_from_text(env.regex, pattern, text, captures, f"step {position} of the chain ({url})")
        chosen, _key = choose(found, step.select, f"Step {position} of the chain ({url})")
        captures = dict(chosen)
        last_url = response.url
    captures["page_url"] = last_url or ""
    return finish(captures, spec.version, spec.download, last_url)


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
