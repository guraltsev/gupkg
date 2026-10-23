"""Provide the one HTTP client that every provider, service and hook shares.

``HttpClient`` sits on ``System.http_request`` and ``System.http_download``, so the
same code runs against the real network and against ``FakeSystem`` fixtures. It
applies one policy everywhere: a gupkg User-Agent (overridable per package with
``headers``), one timeout, retries with backoff on 5xx and 429, redirects followed
hop by hop with scheme and header rules, the ``GITHUB_TOKEN`` sent only to GitHub's
own hosts, size limits on text, ``Content-Disposition`` file names, and resumable
downloads into a cache keyed by URL and expected SHA-256.

Usage and API
-------------
Build ``HttpClient(system, cache_dir=..., env=...)`` once per run. ``text(url)``,
``json(url)`` and ``request(url, method="HEAD")`` read small answers;
``resolve(url)`` follows redirects without downloading the body;
``download(url, expected_sha256=...)`` returns a ``Download`` describing a verified
cached file. ``derive(headers=...)`` returns a client for one package whose headers
override the defaults. Errors are ``HttpError`` (``OSError``); a wrong SHA-256 is
``DownloadMismatch``.

Implementation Approach
-----------------------
Redirects are followed here, never by the transport, because the rules need the
hops: an HTTPS to HTTP downgrade is refused, a redirect to anything but http or
https is refused (``file`` only when the first URL is ``file``), and credentials
(``Authorization``, ``Cookie``) are dropped when the host changes. Request headers
are computed for each hop from the caller's headers plus the policy headers of that
hop's URL. Response bodies for text are capped, and ``Accept-Encoding: identity``
is sent so a compression bomb cannot be requested. A download streams into
``<key>.part``; a server that gave a validator (ETag or Last-Modified) lets a later
call resume with ``Range`` and ``If-Range``, and a finished file is renamed to
``<key>.file`` only after its digest is known.
"""

from __future__ import annotations

import hashlib
import json as jsonlib
import os
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any

from .._version import __version__
from ..report.events import Progress, Step, emit
from ..system.port import HttpResponse, ResponseTooLarge, System
from .safepath import safe_file_name

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.0
MAX_REDIRECTS = 10
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024 * 1024
_MAX_RETRY_AFTER = 30.0
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_RETRY_STATUSES = (429, 500, 502, 503, 504)

# Headers that carry credentials and must not follow a redirect to another host.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
# The only hosts that receive the GITHUB_TOKEN, and only over HTTPS.
_GITHUB_HOSTS = frozenset({"api.github.com", "github.com", "uploads.github.com"})
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class HttpError(OSError):
    """Report a request that failed; ``status`` is the HTTP status, or ``None`` for a network failure."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Build the error from a readable message and an optional HTTP status."""
        super().__init__(message)
        self.status = status


class DownloadMismatch(HttpError):
    """Report that a downloaded file does not have the SHA-256 the caller expected."""


@dataclass(frozen=True)
class Download:
    """Describe a completed download held in the cache.

    Attributes
    ----------
    path : Path
        The cached file. It stays valid until the cache is cleaned; callers read or
        copy it and never modify it.
    size : int
        Size of the file in bytes.
    sha256 : str
        Lower-case SHA-256 digest of the file as observed by this client.
    final_url : str
        The URL that finally served the file, after redirects.
    filename : str
        A harmless file name from ``Content-Disposition`` or the final URL.
    resumed : bool
        Whether part of the file came from an earlier, interrupted attempt.
    from_cache : bool
        Whether no network request was made because a verified file was cached.
    """

    path: Path
    size: int
    sha256: str
    final_url: str
    filename: str
    resumed: bool = False
    from_cache: bool = False


def check_header_map(headers: Mapping[str, str] | None, where: str = "headers") -> dict[str, str]:
    """Return *headers* as a plain dict after rejecting names and values that could split a request.

    Raises
    ------
    ValueError
        If a name is not an HTTP token, or a name or value has a control character
        (header injection).
    """
    checked: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError(f"{where}: names and values must be strings")
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError(f"{where}: {name!r} is not a valid header name")
        if any(ord(character) < 32 and character != "\t" or ord(character) == 127 for character in value):
            raise ValueError(f"{where}: the value of {name!r} has a control character")
        checked[name] = value
    return checked


def parse_content_disposition(value: str | None) -> str | None:
    """Return the file name of a ``Content-Disposition`` header value, or ``None``.

    ``filename*=UTF-8''...`` (RFC 5987) wins over ``filename=``. The result is
    only decoded, not yet made safe: pass it to ``safe_file_name``.
    """
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    # ``get_filename`` decodes both ``filename=`` and the RFC 5987 ``filename*=`` form.
    try:
        name = message.get_filename()
    except (LookupError, ValueError):
        return None
    return name or None


def _host(url: str) -> str:
    """Return the lower-case host name of *url*."""
    return (urllib.parse.urlsplit(url).hostname or "").lower()


class HttpClient:
    """Send every gupkg request with one User-Agent, timeout, retry, redirect and token policy.

    Parameters
    ----------
    system : System
        The boundary that performs the transfers.
    user_agent : str, optional
        The default ``User-Agent``; ``gupkg/<version>`` when omitted.
    timeout : float
        Seconds each request may take before it fails.
    retries : int
        How many times a 5xx, 429 or network failure is retried.
    backoff : float
        Seconds before the first retry; each further retry doubles it.
    headers : mapping, optional
        Default request headers, for example a package's ``headers``; they replace
        the policy headers of the same name (case-insensitive).
    cache_dir : Path, optional
        Where downloads are kept. Required by ``download`` only.
    env : mapping, optional
        The environment to read ``GITHUB_TOKEN`` from; ``os.environ`` when omitted.
    sleep : callable, optional
        Replaces ``time.sleep`` so tests need not wait.
    max_text_bytes : int
        Largest accepted body of a text, JSON or header request.
    """

    def __init__(
        self,
        system: System,
        *,
        user_agent: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        headers: Mapping[str, str] | None = None,
        cache_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_text_bytes: int = MAX_TEXT_BYTES,
    ) -> None:
        """Store the policy; no request is made."""
        self.system = system
        self.user_agent = user_agent or f"gupkg/{__version__}"
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.headers = check_header_map(headers)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.max_text_bytes = max_text_bytes
        self._env = env if env is not None else os.environ
        self._sleep = sleep

    def derive(self, headers: Mapping[str, str] | None = None) -> "HttpClient":
        """Return a client with the same policy whose default headers also include *headers*."""
        merged = {**self.headers, **check_header_map(headers)}
        clone = HttpClient(
            self.system, user_agent=self.user_agent, timeout=self.timeout, retries=self.retries,
            backoff=self.backoff, headers=merged, cache_dir=self.cache_dir, env=self._env,
            sleep=self._sleep, max_text_bytes=self.max_text_bytes,
        )
        return clone

    # -- small answers -------------------------------------------------------

    def request(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        method: str = "GET",
        follow: bool = True,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        """Send one request and return the answer of any status; ``url`` of the result is the final one.

        Parameters
        ----------
        url : str
            An ``http://`` or ``https://`` address (``file://`` is served as is).
        headers : mapping, optional
            Extra headers for this request, overriding the client's defaults.
        method : str
            ``GET`` or ``HEAD``.
        follow : bool
            Follow redirects (at most ``MAX_REDIRECTS`` hops). With ``False`` a 3xx
            answer is returned for the caller to read ``location``.
        max_bytes : int, optional
            Largest accepted body; ``max_text_bytes`` when omitted.

        Raises
        ------
        HttpError
            On a network failure after the retries, a refused redirect, or a body
            larger than allowed.
        """
        limit = self.max_text_bytes if max_bytes is None else max_bytes
        current = url
        extra = check_header_map(headers)
        origin = _host(url)
        for _hop in range(MAX_REDIRECTS + 1):
            response = self._attempt(current, extra, method, limit, _host(current) != origin)
            if not follow or response.status not in _REDIRECT_STATUSES:
                return HttpResponse(response.status, current, response.headers, response.body)
            current = self.redirect_target(current, response)
        raise HttpError(f"More than {MAX_REDIRECTS} redirects from {url}")

    def get(self, url: str, headers: Mapping[str, str] | None = None, *, max_bytes: int | None = None) -> HttpResponse:
        """Return the 2xx answer for *url*, raising ``HttpError`` (with ``status``) for any other status."""
        response = self.request(url, headers, max_bytes=max_bytes)
        if not 200 <= response.status < 300:
            raise HttpError(f"{response.url} answered HTTP {response.status}", response.status)
        return response

    def text(self, url: str, headers: Mapping[str, str] | None = None, *, max_bytes: int | None = None) -> str:
        """Return the text served at *url*, decoded with the declared charset (UTF-8 by default)."""
        response = self.get(url, headers, max_bytes=max_bytes)
        return decode_body(response)

    def json(self, url: str, headers: Mapping[str, str] | None = None, *, max_bytes: int | None = None) -> Any:
        """Return the JSON document served at *url*.

        Raises
        ------
        HttpError
            If the body is not valid JSON or nests too deeply to read.
        """
        text = self.text(url, headers, max_bytes=max_bytes)
        try:
            return jsonlib.loads(text)
        except (ValueError, RecursionError) as error:
            raise HttpError(f"{url} did not serve valid JSON: {error}") from error

    def head(self, url: str, headers: Mapping[str, str] | None = None) -> HttpResponse:
        """Return the headers for *url*, asking again with ``GET`` when the server refuses ``HEAD``."""
        response = self.request(url, headers, method="HEAD")
        if response.status in (405, 501):
            response = self.request(url, headers, method="GET", max_bytes=0)
        return response

    def resolve(self, url: str, headers: Mapping[str, str] | None = None) -> HttpResponse:
        """Follow *url* through its redirects and return the final answer without a body."""
        return self.head(url, headers)

    # -- downloads -----------------------------------------------------------

    def download(
        self,
        url: str,
        *,
        expected_sha256: str | None = None,
        headers: Mapping[str, str] | None = None,
        label: str | None = None,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> Download:
        """Download *url* into the cache, resuming an interrupted attempt, and return the file.

        Parameters
        ----------
        url : str
            Address of the artifact.
        expected_sha256 : str, optional
            When given, a cached file with this digest is returned without any
            request, and a downloaded file with another digest is deleted and
            raises ``DownloadMismatch``. The cache key includes the digest, so a
            changed expectation never reuses an older file.
        headers : mapping, optional
            Extra headers for the request.
        label : str, optional
            The name shown in progress events; the URL's file name by default.
        max_bytes : int
            The download fails when the file would be larger.

        Raises
        ------
        HttpError
            On a network failure after the retries, a refused redirect, a status other
            than 2xx, or a file larger than *max_bytes*.
        DownloadMismatch
            If *expected_sha256* is given and the file's digest differs.
        """
        if self.cache_dir is None:
            raise HttpError("This client has no cache directory, so it cannot download")
        expected = expected_sha256.lower() if expected_sha256 else None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f"{url}\n{expected or ''}".encode("utf-8")).hexdigest()[:40]
        final, part, meta = (self.cache_dir / f"{key}{suffix}" for suffix in (".file", ".part", ".meta.json"))
        shown = label or safe_file_name(urllib.parse.unquote(urllib.parse.urlsplit(url).path), "download")

        # A finished file for exactly this URL and digest needs no network.
        if expected is not None and final.is_file():
            digest, size = file_digest(final)
            if digest == expected:
                emit(Step(f"Using the cached download of {shown}"))
                info = _read_meta(meta)
                return Download(final, size, digest, info.get("final_url", url), info.get("filename", shown), False, True)
            final.unlink()

        # Fetch the body, resuming an interrupted attempt when the server gave a
        # validator; a refused range (416) restarts once from the first byte.
        wanted = check_header_map(headers)
        result = self._download_to(url, part, meta, shown, max_bytes, wanted)
        if result is None:
            part.unlink(missing_ok=True)
            result = self._download_to(url, part, meta, shown, max_bytes, wanted)
        if result is None:
            raise HttpError(f"{url} could not be downloaded from the start")
        final_url, answer_headers, resumed = result

        # Hash the finished file, check its size and expected digest, then publish it.
        info = _read_meta(meta)
        digest, size = file_digest(part)
        if size > max_bytes:
            part.unlink(missing_ok=True)
            raise HttpError(f"{url} is larger than the allowed {max_bytes} bytes")
        if expected is not None and digest != expected:
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            raise DownloadMismatch(f"{info.get('filename', shown)} has SHA-256 {digest}, expected {expected}")
        os.replace(part, final)
        return Download(final, size, digest, final_url, info.get("filename", shown), resumed, False)

    # -- internals -----------------------------------------------------------

    def _policy_headers(self, url: str, extra: Mapping[str, str], foreign: bool = False) -> dict[str, str]:
        """Return the headers for one hop: policy defaults, then client defaults, then the caller's.

        On a hop to another host than the first (*foreign*), credential headers of the client
        and of the caller are left out; the token is decided by the hop's own address.
        """
        headers: dict[str, str] = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}

        # The token goes only to GitHub's own hosts, and only over HTTPS.
        token = self._env.get("GITHUB_TOKEN")
        parts = urllib.parse.urlsplit(url)
        if token and parts.scheme.lower() == "https" and (parts.hostname or "").lower() in _GITHUB_HOSTS:
            headers["Authorization"] = f"Bearer {token}"

        # Later layers replace earlier ones regardless of letter case.
        for layer in (self.headers, extra):
            for name, value in layer.items():
                if foreign and name.lower() in _CREDENTIAL_HEADERS:
                    continue
                for existing in [key for key in headers if key.lower() == name.lower()]:
                    del headers[existing]
                headers[name] = value
        return headers

    def _attempt(self, url: str, extra: Mapping[str, str], method: str, max_bytes: int, foreign: bool = False) -> HttpResponse:
        """Send one hop, retrying a 5xx, 429 or network failure with doubling backoff."""
        delay = self.backoff
        last_error: HttpError | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.system.http_request(
                    url, self._policy_headers(url, extra, foreign), method=method, timeout=self.timeout, max_bytes=max_bytes
                )
            except ResponseTooLarge as error:
                raise HttpError(f"{url} is larger than the allowed {max_bytes} bytes") from error
            except OSError as error:
                last_error = HttpError(f"Could not read {url}: {error}")
            else:
                if response.status not in _RETRY_STATUSES or attempt == self.retries:
                    return response
                last_error = HttpError(f"{url} answered HTTP {response.status}", response.status)
                delay = self._retry_delay(response, delay)
            if attempt < self.retries:
                self._sleep(delay)
                delay *= 2
        assert last_error is not None
        raise last_error

    def _retry_delay(self, response: HttpResponse, current: float) -> float:
        """Return the wait before the next try: ``Retry-After`` seconds (capped) when given."""
        value = response.headers.get("retry-after", "")
        if value.isdigit():
            return min(float(value), _MAX_RETRY_AFTER)
        return current

    def redirect_target(self, url: str, response: HttpResponse) -> str:
        """Return the redirect target of *response*, or raise when the policy refuses it."""
        location = response.headers.get("location")
        if not location:
            raise HttpError(f"{url} redirected without a Location", response.status)
        target = urllib.parse.urljoin(url, location.strip())
        before, after = urllib.parse.urlsplit(url).scheme.lower(), urllib.parse.urlsplit(target).scheme.lower()
        if after not in ("http", "https"):
            raise HttpError(f"{url} redirected to a {after or 'relative'} address, which is refused")
        if before == "https" and after == "http":
            raise HttpError(f"{url} redirected from HTTPS to HTTP, which is refused")
        return target

    def _download_to(
        self, url: str, part: Path, meta: Path, shown: str, max_bytes: int, wanted: Mapping[str, str]
    ) -> tuple[str, Mapping[str, str], bool] | None:
        """Follow redirects and stream the body into *part*; return ``(final url, headers, resumed)``.

        Returns ``None`` when the server refuses the range of a resume (416), so the
        caller can start again from the first byte.
        """
        current = url
        throttle = {"last": time.monotonic()}

        def on_progress(received: int, total: int | None) -> None:
            """Cap the file size, and report progress at most five times a second."""
            if received > max_bytes:
                raise HttpError(f"{url} is larger than the allowed {max_bytes} bytes")
            now = time.monotonic()
            if received == 0 or total == received or now - throttle["last"] >= 0.2:
                emit(Progress(shown, received, total))
                throttle["last"] = now

        def remember(status: int, headers: Mapping[str, str]) -> None:
            """Store the validators and file name of an accepted answer, so an interruption can resume."""
            if not 200 <= status < 300:
                return
            old = _read_meta(meta)
            name = parse_content_disposition(headers.get("content-disposition"))
            filename = safe_file_name(name or urllib.parse.unquote(urllib.parse.urlsplit(current).path), "download")
            fresh = {
                "url": url, "final_url": current, "filename": filename if name or "filename" not in old else old["filename"],
                "etag": headers.get("etag", ""), "last_modified": headers.get("last-modified", ""),
            }
            if status == 206:
                fresh = {key: value or old.get(key, "") for key, value in fresh.items()}
            _write_meta(meta, fresh)

        for _hop in range(MAX_REDIRECTS + 1):
            delay = self.backoff
            response = None
            for attempt in range(self.retries + 1):
                # Decide per attempt whether to resume: only a partial file whose
                # validator is known is continued, otherwise it starts over.
                info = _read_meta(meta)
                validator = info.get("etag") or info.get("last_modified")
                offset = part.stat().st_size if part.is_file() and validator and info.get("url") == url else 0
                if not offset:
                    part.unlink(missing_ok=True)
                extra = {**wanted, "Range": f"bytes={offset}-", "If-Range": validator} if offset else dict(wanted)
                try:
                    response = self.system.http_download(
                        current, self._policy_headers(current, extra, _host(current) != _host(url)), part, on_progress,
                        timeout=self.timeout, append=bool(offset), on_headers=remember,
                    )
                except HttpError:
                    raise
                except OSError as error:
                    if attempt == self.retries:
                        raise HttpError(f"Could not download {current}: {error}") from error
                    self._sleep(delay)
                    delay *= 2
                    continue
                if response.status in _RETRY_STATUSES and attempt < self.retries:
                    delay = self._retry_delay(HttpResponse(response.status, current, response.headers), delay)
                    self._sleep(delay)
                    delay *= 2
                    continue
                break
            assert response is not None
            if response.status in _REDIRECT_STATUSES:
                current = self.redirect_target(current, HttpResponse(response.status, current, response.headers))
                continue
            if response.status == 416:
                return None
            if not 200 <= response.status < 300:
                raise HttpError(f"{current} answered HTTP {response.status}", response.status)
            emit(Progress(shown, part.stat().st_size, part.stat().st_size))
            return current, response.headers, response.resumed
        raise HttpError(f"More than {MAX_REDIRECTS} redirects from {url}")


# ---------------------------------------------------------------------------
# Helpers shared with the other services
# ---------------------------------------------------------------------------


def decode_body(response: HttpResponse) -> str:
    """Return the body of *response* as text, using the charset of ``Content-Type`` (UTF-8 by default).

    Undecodable bytes become U+FFFD and a leading byte-order mark is dropped, so a
    hostile page can never raise from here.
    """
    match = re.search(r"charset=\"?([\w.-]+)", response.headers.get("content-type", ""), re.IGNORECASE)
    charset = match.group(1) if match else "utf-8"
    try:
        text = response.body.decode(charset, errors="replace")
    except LookupError:
        text = response.body.decode("utf-8", errors="replace")
    return text.lstrip("﻿")


def file_digest(path: Path) -> tuple[str, int]:
    """Return ``(lower-case SHA-256, size)`` of a file, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _read_meta(path: Path) -> dict[str, str]:
    """Return the cached facts about a download, or an empty dict when absent or damaged."""
    try:
        data = jsonlib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, str)} if isinstance(data, dict) else {}


def _write_meta(path: Path, data: Mapping[str, str]) -> None:
    """Store the facts about a download next to it."""
    path.write_text(jsonlib.dumps(dict(data)), encoding="utf-8")
