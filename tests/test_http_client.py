"""Cover the shared HTTP client: User-Agent, headers, retries, redirects, token and downloads.

The client is real; the boundary it sits on is ``FakeSystem``, which serves scripted answers
(status, headers, interruptions, ``Range`` support) and records every request it receives.
The wait between retries is replaced by a recording function so no test sleeps. Out of
scope: the real transport (``test_http_transport``) and the providers that use the client.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gupkg._version import __version__
from gupkg.services.http import (
    DownloadMismatch,
    HttpClient,
    HttpError,
    check_header_map,
    parse_content_disposition,
)
from gupkg.system.fake import FakeHttp, FakeSystem

URL = "https://example.org/data"
BODY = bytes(range(256)) * 600  # 153,600 bytes: more than one 64 KiB chunk


def make_client(system: FakeSystem, tmp_path: Path, *, env: dict[str, str] | None = None, **options) -> tuple[HttpClient, list[float]]:
    """Return a client over *system* and the list that records each wait it would have made."""
    waits: list[float] = []
    client = HttpClient(system, env=env or {}, sleep=waits.append, cache_dir=tmp_path / "cache", **options)
    return client, waits


# ---------------------------------------------------------------------------
# Headers and policy
# ---------------------------------------------------------------------------


def test_every_request_carries_the_gupkg_user_agent_the_timeout_and_no_compression(system: FakeSystem, tmp_path: Path) -> None:
    """The default User-Agent names gupkg and its version, the timeout is the client's, and compression is declined."""
    system.urls[URL] = "hello"
    client, _ = make_client(system, tmp_path, timeout=12.5)

    assert client.text(URL) == "hello"

    sent = system.request_log[0]
    assert sent.headers["User-Agent"] == f"gupkg/{__version__}"
    assert sent.headers["Accept-Encoding"] == "identity"
    assert sent.timeout == 12.5


def test_package_headers_replace_the_default_user_agent_whatever_their_letter_case(system: FakeSystem, tmp_path: Path) -> None:
    """A package's `headers` win over the policy headers, and a per-request header wins over both."""
    system.urls[URL] = "x"
    client, _ = make_client(system, tmp_path, headers={"user-agent": "Mozilla/5.0", "Referer": "https://example.org/"})

    client.text(URL)
    client.text(URL, {"Referer": "https://other.example/"})

    first, second = (request.headers for request in system.request_log)
    assert first == {"Accept-Encoding": "identity", "user-agent": "Mozilla/5.0", "Referer": "https://example.org/"}
    assert second["Referer"] == "https://other.example/" and second["user-agent"] == "Mozilla/5.0"


@pytest.mark.parametrize(
    ("headers", "problem"),
    [
        pytest.param({"X-Test": "a\r\nInjected: yes"}, "control character", id="value-with-a-line-break"),
        pytest.param({"Bad Name": "x"}, "not a valid header name", id="name-with-a-space"),
        pytest.param({"X-Test": 5}, "must be strings", id="non-string-value"),
    ],
)
def test_headers_that_could_split_a_request_are_refused(headers: dict, problem: str) -> None:
    """A header name or value that is not a plain token or text is rejected before it can reach a request."""
    with pytest.raises(ValueError, match=problem):
        check_header_map(headers)


def test_the_github_token_goes_only_to_github_over_https(system: FakeSystem, tmp_path: Path) -> None:
    """GITHUB_TOKEN is sent to api.github.com but not to another host, and never over plain http."""
    for address in ("https://api.github.com/x", "https://example.org/x", "http://api.github.com/x"):
        system.urls[address] = "{}"
    client, _ = make_client(system, tmp_path, env={"GITHUB_TOKEN": "s3cret"})

    for address in ("https://api.github.com/x", "https://example.org/x", "http://api.github.com/x"):
        client.text(address)

    sent = {request.url: request.headers for request in system.request_log}
    assert sent["https://api.github.com/x"]["Authorization"] == "Bearer s3cret"
    assert "Authorization" not in sent["https://example.org/x"]
    assert "Authorization" not in sent["http://api.github.com/x"]


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_a_server_error_is_retried_with_doubling_backoff_until_it_succeeds(system: FakeSystem, tmp_path: Path) -> None:
    """Two 503 answers are retried after 1 s and 2 s, and the third attempt's body is returned."""
    answers = iter([FakeHttp("", 503), FakeHttp("", 503), FakeHttp("finally")])
    system.urls[URL] = lambda url, headers: next(answers)
    client, waits = make_client(system, tmp_path)

    assert client.text(URL) == "finally"
    assert waits == [1.0, 2.0]


def test_retry_after_is_honoured_up_to_a_cap_and_429_is_retried(system: FakeSystem, tmp_path: Path) -> None:
    """A 429 with `Retry-After: 7` waits 7 s; a huge value is capped at 30 s."""
    answers = iter([FakeHttp("", 429, {"Retry-After": "7"}), FakeHttp("", 429, {"Retry-After": "99999"}), FakeHttp("ok")])
    system.urls[URL] = lambda url, headers: next(answers)
    client, waits = make_client(system, tmp_path)

    assert client.text(URL) == "ok"
    assert waits == [7.0, 30.0]


def test_persistent_server_errors_fail_with_the_status_after_the_configured_retries(system: FakeSystem, tmp_path: Path) -> None:
    """After `retries` more attempts the 503 is reported as an HttpError carrying the status."""
    system.urls[URL] = FakeHttp("", 503)
    client, waits = make_client(system, tmp_path, retries=2)

    with pytest.raises(HttpError) as raised:
        client.text(URL)

    assert raised.value.status == 503
    assert len(system.request_log) == 3 and len(waits) == 2


def test_a_client_error_is_not_retried(system: FakeSystem, tmp_path: Path) -> None:
    """A 404 fails at once: retrying cannot make a missing page appear."""
    system.urls[URL] = FakeHttp("gone", 404)
    client, waits = make_client(system, tmp_path)

    with pytest.raises(HttpError, match="HTTP 404"):
        client.text(URL)

    assert len(system.request_log) == 1 and waits == []


def test_a_dropped_connection_is_retried_then_reported_as_http_error(system: FakeSystem, tmp_path: Path) -> None:
    """A network failure is retried, and when it persists the error names the address."""
    system.urls[URL] = ConnectionResetError("reset by peer")
    client, _ = make_client(system, tmp_path, retries=1)

    with pytest.raises(HttpError, match="Could not read https://example.org/data"):
        client.text(URL)

    assert len(system.request_log) == 2


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


def test_redirects_are_followed_hop_by_hop_including_relative_locations(system: FakeSystem, tmp_path: Path) -> None:
    """A relative Location is resolved against the current address, and the final URL is reported."""
    system.urls["https://example.org/a"] = FakeHttp("", 302, {"Location": "/b"})
    system.urls["https://example.org/b"] = FakeHttp("", 301, {"Location": "https://cdn.example.net/c"})
    system.urls["https://cdn.example.net/c"] = FakeHttp("payload")
    client, _ = make_client(system, tmp_path)

    response = client.get("https://example.org/a")

    assert response.url == "https://cdn.example.net/c" and response.body == b"payload"


@pytest.mark.parametrize(
    ("location", "message"),
    [
        pytest.param("http://example.org/plain", "HTTPS to HTTP", id="downgrade-to-http"),
        pytest.param("file:///C:/Windows/win.ini", "file address", id="to-a-local-file"),
        pytest.param("ftp://example.org/x", "ftp address", id="to-ftp"),
    ],
)
def test_a_redirect_that_downgrades_or_leaves_http_is_refused(system: FakeSystem, tmp_path: Path, location: str, message: str) -> None:
    """HTTPS never silently becomes HTTP, and a redirect never reaches a file or another protocol."""
    system.urls["https://example.org/a"] = FakeHttp("", 302, {"Location": location})
    client, _ = make_client(system, tmp_path)

    with pytest.raises(HttpError, match=message):
        client.get("https://example.org/a")


def test_credentials_do_not_follow_a_redirect_to_another_host(system: FakeSystem, tmp_path: Path) -> None:
    """The GITHUB_TOKEN and a caller's Authorization or Cookie are dropped when the host changes."""
    system.urls["https://api.github.com/start"] = FakeHttp("", 302, {"Location": "https://objects.githubusercontent.com/blob"})
    system.urls["https://objects.githubusercontent.com/blob"] = FakeHttp("data")
    client, _ = make_client(system, tmp_path, env={"GITHUB_TOKEN": "s3cret"})

    client.get("https://api.github.com/start", {"Authorization": "Basic abc", "Cookie": "a=b"})

    first, second = (request.headers for request in system.request_log)
    assert first["Authorization"] == "Basic abc" and first["Cookie"] == "a=b"
    assert "Authorization" not in second and "Cookie" not in second


def test_a_redirect_loop_stops_after_ten_hops(system: FakeSystem, tmp_path: Path) -> None:
    """An endless redirect chain fails with a clear error instead of running forever."""
    system.urls["https://example.org/loop"] = FakeHttp("", 302, {"Location": "https://example.org/loop"})
    client, _ = make_client(system, tmp_path)

    with pytest.raises(HttpError, match="More than 10 redirects"):
        client.get("https://example.org/loop")


def test_resolve_follows_redirects_with_head_and_falls_back_to_get_when_head_is_refused(system: FakeSystem, tmp_path: Path) -> None:
    """`resolve` never downloads the body: it asks with HEAD, and with a zero-byte GET if the server says 405."""
    system.urls["https://example.org/dl"] = lambda url, headers: FakeHttp("", 405)
    client, _ = make_client(system, tmp_path)

    response = client.resolve("https://example.org/dl")

    assert response.status == 405
    assert [(request.method, request.max_bytes) for request in system.request_log] == [("HEAD", client.max_text_bytes), ("GET", 0)]


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


def test_a_text_body_larger_than_the_cap_is_refused(system: FakeSystem, tmp_path: Path) -> None:
    """A hostile server cannot make a provider hold an unbounded page in memory."""
    system.urls[URL] = "x" * 5000
    client, _ = make_client(system, tmp_path, max_text_bytes=1000)

    with pytest.raises(HttpError, match="larger than the allowed 1000 bytes"):
        client.text(URL)


def test_text_uses_the_declared_charset_and_json_errors_are_http_errors(system: FakeSystem, tmp_path: Path) -> None:
    """Latin-1 text decodes with its declared charset; invalid JSON is an HttpError, not a stray ValueError."""
    system.urls[URL] = FakeHttp("café".encode("latin-1"), headers={"Content-Type": "text/html; charset=ISO-8859-1"})
    system.urls[URL + "/j"] = FakeHttp("{broken")
    client, _ = make_client(system, tmp_path)

    assert client.text(URL) == "café"
    with pytest.raises(HttpError, match="valid JSON"):
        client.json(URL + "/j")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param('attachment; filename="tool-1.0.zip"', "tool-1.0.zip", id="quoted"),
        pytest.param("attachment; filename=tool.zip", "tool.zip", id="bare"),
        pytest.param("attachment; filename*=UTF-8''na%C3%AFve%20tool.zip", "naïve tool.zip", id="rfc5987"),
        pytest.param(None, None, id="absent"),
        pytest.param("inline", None, id="no-filename"),
    ],
)
def test_content_disposition_names_are_decoded(header: str | None, expected: str | None) -> None:
    """Both the plain and the RFC 5987 forms of a `Content-Disposition` file name are read."""
    assert parse_content_disposition(header) == expected


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def test_a_download_is_cached_by_url_and_digest_and_a_second_call_needs_no_request(system: FakeSystem, tmp_path: Path) -> None:
    """With the right expected SHA-256 the cached file is returned without touching the network."""
    system.urls[URL] = FakeHttp(BODY, headers={"Content-Disposition": 'attachment; filename="tool.zip"'})
    client, _ = make_client(system, tmp_path)
    digest = hashlib.sha256(BODY).hexdigest()

    first = client.download(URL, expected_sha256=digest)
    second = client.download(URL, expected_sha256=digest.upper())

    assert first.path.read_bytes() == BODY and first.sha256 == digest and first.filename == "tool.zip"
    assert second.from_cache and second.path == first.path and second.filename == "tool.zip"
    assert len(system.request_log) == 1


def test_a_download_with_the_wrong_digest_is_deleted_and_reported(system: FakeSystem, tmp_path: Path) -> None:
    """A mismatch raises DownloadMismatch and leaves no cached file that a later run could trust."""
    system.urls[URL] = FakeHttp(BODY)
    client, _ = make_client(system, tmp_path)

    with pytest.raises(DownloadMismatch, match="expected " + "0" * 64):
        client.download(URL, expected_sha256="0" * 64)

    assert not [path for path in (tmp_path / "cache").iterdir() if path.suffix == ".file"]


def test_a_changed_expected_digest_never_reuses_an_older_cached_file(system: FakeSystem, tmp_path: Path) -> None:
    """The cache key includes the digest: a different expectation downloads again and fails on its own terms."""
    system.urls[URL] = FakeHttp(BODY)
    client, _ = make_client(system, tmp_path)
    client.download(URL, expected_sha256=hashlib.sha256(BODY).hexdigest())

    with pytest.raises(DownloadMismatch):
        client.download(URL, expected_sha256="1" * 64)

    assert len(system.request_log) == 2


def test_an_interrupted_download_resumes_with_range_and_if_range(system: FakeSystem, tmp_path: Path) -> None:
    """After a dropped connection the partial file is continued from its size, guarded by the ETag."""
    calls = iter([
        FakeHttp(BODY, headers={"ETag": '"v1"'}, ranges=True, fail_after=70_000),
        FakeHttp(BODY, headers={"ETag": '"v1"'}, ranges=True),
    ])
    system.urls[URL] = lambda url, headers: next(calls)
    client, _ = make_client(system, tmp_path)

    result = client.download(URL, expected_sha256=hashlib.sha256(BODY).hexdigest())

    assert result.path.read_bytes() == BODY and result.resumed
    second = system.request_log[1].headers
    assert second["Range"] == "bytes=70000-" and second["If-Range"] == '"v1"'


def test_a_partial_file_without_a_validator_is_never_resumed(system: FakeSystem, tmp_path: Path) -> None:
    """A server that gave no ETag or Last-Modified cannot prove the file is unchanged, so the download starts over."""
    calls = iter([FakeHttp(BODY, ranges=True, fail_after=70_000), FakeHttp(BODY, ranges=True)])
    system.urls[URL] = lambda url, headers: next(calls)
    client, _ = make_client(system, tmp_path)

    result = client.download(URL)

    assert result.path.read_bytes() == BODY and not result.resumed
    assert "Range" not in system.request_log[1].headers


def test_a_refused_range_restarts_the_download_from_the_first_byte(system: FakeSystem, tmp_path: Path) -> None:
    """If the server answers 416 to a resume, the partial file is discarded and the whole file is fetched."""
    calls = iter([
        FakeHttp(BODY, headers={"ETag": '"v1"'}, fail_after=70_000),
        FakeHttp("", 416),
        FakeHttp(BODY, headers={"ETag": '"v1"'}),
    ])
    system.urls[URL] = lambda url, headers: next(calls)
    client, _ = make_client(system, tmp_path)

    result = client.download(URL)

    assert result.path.read_bytes() == BODY and not result.resumed


def test_downloads_follow_redirects_under_the_same_rules(system: FakeSystem, tmp_path: Path) -> None:
    """A download redirected to another host succeeds there, and its file name comes from the final response."""
    system.urls[URL] = FakeHttp("", 302, {"Location": "https://cdn.example.net/blob"})
    system.urls["https://cdn.example.net/blob"] = FakeHttp(BODY, headers={"Content-Disposition": "attachment; filename*=UTF-8''..\\evil.exe"})
    client, _ = make_client(system, tmp_path)

    result = client.download(URL)

    assert result.final_url == "https://cdn.example.net/blob"
    assert result.filename == "evil.exe"


def test_a_download_larger_than_the_cap_is_aborted(system: FakeSystem, tmp_path: Path) -> None:
    """The size limit stops a transfer while it runs, not after the disk is full."""
    system.urls[URL] = FakeHttp(BODY)
    client, _ = make_client(system, tmp_path)

    with pytest.raises(HttpError, match="larger than the allowed"):
        client.download(URL, max_bytes=100_000)


def test_a_download_needs_a_cache_directory(system: FakeSystem) -> None:
    """A client built without a cache directory refuses to download rather than choosing a place."""
    client = HttpClient(system, env={})

    with pytest.raises(HttpError, match="no cache directory"):
        client.download(URL)
