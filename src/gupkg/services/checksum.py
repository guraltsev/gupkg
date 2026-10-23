"""Discover the SHA-256 of a version's download from what the vendor publishes.

A tracker's ``[source.checksum]`` says where the vendor puts the digest: next to the file
(``sidecar``), as a line of a sums file (``sums_file`` with ``entry``), in a JSON document
(``json`` with ``path``), or on a page (``page_match``). GitHub and GitLab digests that
came with the release are picked up implicitly by the track providers. Discovery only
*finds* a digest and names where it came from; deciding what a missing digest means, and
verifying the downloaded file, belong to the checksum policy (issue 027).

Usage and API
-------------
``discover_checksum(client, checksum, values)`` returns a ``DiscoveredChecksum`` or raises
``ChecksumDiscoveryError``. ``find_digest(text, name)`` reads a digest out of sums-file
text and is re-exported to hooks as ``gupkg.hooklib.find_digest``.

Implementation Approach
-----------------------
Every address is a template expanded from the tracker's captures and must be http or
https. Text is parsed line by line with anchored patterns (linear time) and only 64-digit
hexadecimal tokens are accepted, so a hostile page cannot smuggle in anything else.
Digests are lower-cased; the provenance is always ``vendor-sums``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ..model.jsonpath import JsonPathError, parse_path
from ..model.manifest import Checksum, DiscoveredChecksum
from ..model.patterns import PatternError, scan
from .http import HttpClient, decode_body
from .values import TrackError, absolute_url, compile_runtime, expand

_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_GNU_LINE = re.compile(r"^\s*(?P<digest>[0-9A-Fa-f]{64})\s+[ *]?(?P<name>\S.*?)\s*$")
_BSD_LINE = re.compile(r"^\s*SHA256\s*\((?P<name>.+)\)\s*=\s*(?P<digest>[0-9A-Fa-f]{64})\s*$", re.IGNORECASE)
_MAX_LINES = 200_000


class ChecksumDiscoveryError(RuntimeError):
    """Report that a configured checksum source could not supply a digest."""


def discover_checksum(client: HttpClient, checksum: Checksum, values: Mapping[str, str]) -> DiscoveredChecksum:
    """Fetch the digest *checksum* describes, expanding its templates from *values*.

    Parameters
    ----------
    client : HttpClient
        The shared HTTP client.
    checksum : Checksum
        The ``[source.checksum]`` table of the tracker.
    values : mapping
        ``version``, the provider's captures, and ``download_url`` (the address that will be
        downloaded). ``page_url`` is needed for ``page_match``.

    Returns
    -------
    DiscoveredChecksum
        The lower-case digest, with provenance ``vendor-sums``.

    Raises
    ------
    ChecksumDiscoveryError
        If the source cannot be read or does not contain a usable digest.
    """
    try:
        if checksum.sidecar is not None:
            url = absolute_url(expand(checksum.sidecar, values))
            text = _text(client, url)
            wanted = _basename(values.get("download_url", ""))
            digest = find_digest(text, wanted) or _only_digest(text)
            where = url
        elif checksum.sums_file is not None:
            url = absolute_url(expand(checksum.sums_file, values))
            text = _text(client, url)
            entry = expand(checksum.entry or "", values)
            digest = find_digest(text, entry)
            where = f"{url} (entry {entry!r})"
        elif checksum.json is not None:
            url = absolute_url(expand(checksum.json, values))
            document = client.json(url)
            nodes = parse_path(expand(checksum.path or "", values)).values(document)
            digest = next((_clean(str(node)) for node in nodes if isinstance(node, str) and _HEX.search(_clean(node))), None)
            where = url
        else:
            page = values.get("page_url")
            if not page:
                raise ChecksumDiscoveryError("page_match needs a provider that reads a page; this one has none")
            text = _text(client, page)
            pattern = compile_runtime(checksum.page_match or "", values)
            found = scan(pattern, text, 100)
            digest = None
            for match in found:
                groups = match.groupdict()
                candidate = groups.get("sha256") or (match.group(1) if match.groups() else match.group(0))
                if candidate and _HEX.fullmatch(candidate.strip()):
                    digest = candidate.strip()
                    break
            where = page
    except (OSError, TrackError, JsonPathError, PatternError) as error:
        raise ChecksumDiscoveryError(f"Could not discover the checksum: {error}") from error
    if digest is None:
        raise ChecksumDiscoveryError(f"{where} does not contain a SHA-256 for this download")
    return DiscoveredChecksum(digest.lower(), "vendor-sums")


def find_digest(text: str, name: str | None) -> str | None:
    """Return the SHA-256 listed for *name* in sums-file text, or ``None``.

    Both ``<digest>  <name>`` (also with ``*`` before the name) and ``SHA256 (<name>) =
    <digest>`` lines are read. Names compare exactly after dropping a leading ``./`` and
    turning ``\\`` into ``/``. With no *name*, a text holding exactly one digest returns it.
    """
    if name is None:
        return _only_digest(text)
    wanted = _normal(name)
    for count, line in enumerate(text.splitlines()):
        if count > _MAX_LINES:
            break
        match = _GNU_LINE.match(line) or _BSD_LINE.match(line)
        if match is not None and _normal(match.group("name")) == wanted:
            return match.group("digest").lower()
    return None


def _only_digest(text: str) -> str | None:
    """Return the digest when *text* holds exactly one distinct 64-digit token, else ``None``."""
    found = {token.lower() for token in _HEX.findall(text[:1_000_000])}
    return next(iter(found)) if len(found) == 1 else None


def _normal(name: str) -> str:
    """Return a sums-file name in comparison form."""
    cleaned = name.strip().replace("\\", "/")
    return cleaned[2:] if cleaned.startswith("./") else cleaned


def _basename(url: str) -> str | None:
    """Return the last path segment of *url*, or ``None`` when there is none."""
    segment = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return segment or None


def _clean(value: str) -> str:
    """Return a digest string without a ``sha256:`` prefix."""
    return value.strip().removeprefix("sha256:").strip()


def _text(client: HttpClient, url: str) -> str:
    """Fetch *url* as text through the shared client."""
    return decode_body(client.get(url))
