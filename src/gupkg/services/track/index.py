"""Discover a version by walking down directory listings, and by following redirects.

``track_index`` reads a listing page, picks the best entry of each ``[[track.level]]``
(newest first), and descends into it; a level may ``probe`` for a file and fall back to
the next best entry when it does not exist (a release folder that is not yet complete).
``track_redirect`` resolves a stable download link hop by hop without downloading the
file and reads the version from the final address.

Usage and API
-------------
``track_index(spec, env)`` and ``track_redirect(spec, env)`` return a ``TrackResult`` or
raise ``TrackError``.

Implementation Approach
-----------------------
The index walk is a depth-first search that tries candidates best-first and backtracks,
under a hard budget of requests so a hostile listing cannot make a check run forever.
The redirect resolver issues one ``HEAD`` per hop with redirects switched off in the
client, so the provider sees every ``Location``, can unwrap a ``url=`` query parameter
and still gets the client's scheme and downgrade rules.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping

from ...model.manifest import TrackResult
from ...model.trackspec import IndexSpec, RedirectSpec
from ...system.port import HttpResponse
from ..http import MAX_REDIRECTS, HttpError, decode_body
from .common import (
    TrackEnvironment,
    TrackError,
    absolute_url,
    candidates_from_text,
    compile_runtime,
    expand,
    finish,
    ordered,
)

MAX_REQUESTS = 60
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def track_index(spec: IndexSpec, env: TrackEnvironment) -> TrackResult:
    """Walk the levels of *spec* from ``check_url`` and return the version found at the bottom.

    Raises
    ------
    TrackError
        If a page cannot be read, no entry leads to a complete result, or more than
        ``MAX_REQUESTS`` requests would be needed.
    """
    budget = {"left": MAX_REQUESTS}

    def request_page(url: str) -> tuple[str, str]:
        """Fetch one listing page within the budget; return its text and final URL."""
        if budget["left"] <= 0:
            raise TrackError(f"The index walk needed more than {MAX_REQUESTS} requests")
        budget["left"] -= 1
        try:
            response = env.http.get(url, spec.headers)
        except OSError as error:
            raise TrackError(str(error)) from error
        return decode_body(response), response.url

    def exists(url: str) -> bool:
        """Return whether *url* answers with a 2xx status, within the budget."""
        if budget["left"] <= 0:
            raise TrackError(f"The index walk needed more than {MAX_REQUESTS} requests")
        budget["left"] -= 1
        try:
            return 200 <= env.http.head(url, spec.headers).status < 300
        except OSError:
            return False

    def walk(level_number: int, url: str, captures: Mapping[str, str]) -> tuple[Mapping[str, str], str] | None:
        """Try the entries of one level best-first; return the first complete path or ``None``."""
        level = spec.levels[level_number]
        text, page_url = request_page(url)
        pattern = compile_runtime(level.match_text, captures)
        found = candidates_from_text(pattern, text, captures)
        if level.key is not None:
            found = [(values, expand(level.key, values)) for values, _key in found]
        for values, _key in ordered(found, level.select):
            # An entry only counts when its probe file exists.
            if level.probe is not None and not exists(urllib.parse.urljoin(page_url, expand(level.probe, values))):
                continue
            if level_number == len(spec.levels) - 1:
                return values, page_url
            deeper = walk(level_number + 1, urllib.parse.urljoin(page_url, expand(level.next or "", values)), values)
            if deeper is not None:
                return deeper
        return None

    result = walk(0, spec.check_url, {})
    if result is None:
        raise TrackError(f"The index at {spec.check_url} has no entry that matches every level")
    captures, page_url = result
    return finish({**captures, "page_url": page_url}, spec.version, spec.download, page_url)


def track_redirect(spec: RedirectSpec, env: TrackEnvironment) -> TrackResult:
    """Resolve ``check_url`` through its redirects and read the version from the final address.

    Raises
    ------
    TrackError
        If a hop fails, the redirect policy refuses a hop, there are too many hops, or
        the final address does not match ``match``.
    """
    url = spec.check_url
    final: str | None = None
    for _hop in range(MAX_REDIRECTS):
        response = _head(env, url, spec.headers)
        if response.status in _REDIRECT_STATUSES:
            try:
                target = env.http.redirect_target(url, response)
            except HttpError as error:
                raise TrackError(str(error)) from error

            # Some sites wrap the real file address in a ``url=`` query parameter.
            if spec.unwrap_query:
                wrapped = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query).get(spec.unwrap_query)
                if wrapped:
                    final = absolute_url(wrapped[0], url)
                    break
            url = target
            continue
        if not 200 <= response.status < 300:
            raise TrackError(f"{url} answered HTTP {response.status}")
        final = url
        break
    if final is None:
        raise TrackError(f"{spec.check_url} redirects more than {MAX_REDIRECTS} times")

    # The address itself carries the version; the file name is offered as a capture.
    name = urllib.parse.unquote(urllib.parse.urlsplit(final).path.rstrip("/").rsplit("/", 1)[-1])
    captures: dict[str, str] = {"page_url": spec.check_url, "final_url": final, "download_url": final, "file_name": name}
    if spec.match is not None:
        found = candidates_from_text(spec.match, final, captures)
        if not found:
            raise TrackError(f"The final address {final} does not match the pattern")
        captures = dict(found[0][0])
    return finish(captures, spec.version, spec.download, final)


def _head(env: TrackEnvironment, url: str, headers: Mapping[str, str]) -> HttpResponse:
    """Send one ``HEAD`` without following redirects, asking again with ``GET`` when ``HEAD`` is refused."""
    try:
        response = env.http.request(url, headers, method="HEAD", follow=False)
        if response.status in (405, 501):
            response = env.http.request(url, headers, method="GET", follow=False, max_bytes=0)
    except OSError as error:
        raise TrackError(str(error)) from error
    return response
