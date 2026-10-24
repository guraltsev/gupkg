"""Cover the HTTP transport contract: the real ``WindowsSystem`` and ``FakeSystem`` give the same answers.

The real transport talks to a local loopback server (a thread on 127.0.0.1 with scripted
routes; nothing leaves the machine). ``FakeSystem`` serves the same routes from fixtures. The
contract tests run on both, so a fixture cannot promise behavior that the real stack lacks, and
the shared client is also run end to end over real sockets. Out of scope: TLS (no certificate is
involved) and proxies.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gupkg.services.http import HttpClient, HttpError
from gupkg.system.fake import FakeHttp, FakeSystem
from gupkg.system.port import ResponseTooLarge, System
from gupkg.system.windows import WindowsSystem

PAYLOAD = bytes(range(256)) * 400  # 102,400 bytes


class Handler(BaseHTTPRequestHandler):
    """Serve the scripted routes of the transport tests; the server records each request line."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature of the base class
        """Keep the test output clean."""

    def do_HEAD(self) -> None:  # noqa: N802 - the name is the HTTP method
        """Answer HEAD like GET without a body."""
        self.route(send_body=False)

    def do_GET(self) -> None:  # noqa: N802 - the name is the HTTP method
        """Answer GET."""
        self.route(send_body=True)

    def route(self, *, send_body: bool) -> None:
        """Dispatch on the path."""
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        path = self.path.split("?")[0]
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/data")
            self.end_headers()
        elif path == "/missing":
            body = b"not here"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
        elif path == "/data":
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.send_header("ETag", '"abc"')
            self.send_header("Content-Disposition", 'attachment; filename="payload.bin"')
            self.end_headers()
            if send_body:
                self.wfile.write(PAYLOAD)
        elif path == "/ranged":
            start = 0
            header = self.headers.get("Range")
            if header and header.startswith("bytes=") and header.endswith("-"):
                start = int(header[6:-1])
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(PAYLOAD) - start))
            self.send_header("ETag", '"abc"')
            self.end_headers()
            if send_body:
                self.wfile.write(PAYLOAD[start:])
        else:
            self.send_response(500)
            self.end_headers()


@pytest.fixture
def server() -> Iterator[ThreadingHTTPServer]:
    """A loopback HTTP server whose `seen` list records (method, path, headers) of every request."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.seen = []
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def base(server: ThreadingHTTPServer) -> str:
    """Return the server's base address."""
    return f"http://127.0.0.1:{server.server_address[1]}"


FAKE_BASE = "http://fake.test"


@pytest.fixture(params=["windows", "fake"], ids=["windows-loopback", "fake-system"])
def transport(request: pytest.FixtureRequest, server: ThreadingHTTPServer, tmp_path: Path) -> tuple[System, str]:
    """A ``(system, base address)`` pair: the real transport against the loopback server, or a FakeSystem serving the same routes."""
    if request.param == "windows":
        return WindowsSystem(), base(server)
    system = FakeSystem(tmp_path / "machine")
    system.urls.update({
        f"{FAKE_BASE}/redirect": FakeHttp("", 302, {"Location": "/data"}),
        f"{FAKE_BASE}/missing": FakeHttp("not here", 404),
        f"{FAKE_BASE}/data": FakeHttp(PAYLOAD, headers={"ETag": '"abc"', "Content-Disposition": 'attachment; filename="payload.bin"'}),
        f"{FAKE_BASE}/ranged": FakeHttp(PAYLOAD, headers={"ETag": '"abc"'}, ranges=True),
    })
    return system, FAKE_BASE


def test_http_request_does_not_follow_a_redirect_and_returns_any_status(transport: tuple[System, str]) -> None:
    """The transport hands back the 302 with its Location and a 404 with its body, instead of raising or following."""
    system, address = transport

    moved = system.http_request(address + "/redirect")
    missing = system.http_request(address + "/missing")

    assert moved.status == 302 and moved.headers["location"] == "/data"
    assert missing.status == 404 and missing.body == b"not here"


def test_http_request_enforces_the_size_cap_and_supports_head_and_zero_bytes(transport: tuple[System, str]) -> None:
    """A body over the cap raises ResponseTooLarge; HEAD and `max_bytes=0` return headers with no body."""
    system, address = transport

    with pytest.raises(ResponseTooLarge):
        system.http_request(address + "/data", max_bytes=1000)
    head = system.http_request(address + "/data", method="HEAD")
    headers_only = system.http_request(address + "/data", max_bytes=0)

    assert head.body == b"" and head.headers["etag"] == '"abc"'
    assert headers_only.body == b"" and headers_only.headers["content-length"] == str(len(PAYLOAD))


def test_http_download_writes_a_whole_file_and_reports_progress(transport: tuple[System, str], tmp_path: Path) -> None:
    """A 200 answer is streamed to the file, `on_headers` sees the status first, and progress ends at the full size."""
    system, address = transport
    seen: list[tuple[int, int | None]] = []
    heard: list[int] = []

    result = system.http_download(
        address + "/data", None, tmp_path / "out.bin", lambda done, total: seen.append((done, total)),
        on_headers=lambda status, headers: heard.append(status),
    )

    assert (tmp_path / "out.bin").read_bytes() == PAYLOAD and result.written == len(PAYLOAD) and not result.resumed
    assert heard == [200] and seen[0] == (0, len(PAYLOAD)) and seen[-1] == (len(PAYLOAD), len(PAYLOAD))


def test_http_download_appends_a_206_answer_and_replaces_on_a_plain_200(transport: tuple[System, str], tmp_path: Path) -> None:
    """With `append=True` a 206 continues the file; a server that ignores the range replaces it."""
    system, address = transport
    target = tmp_path / "out.bin"
    target.write_bytes(PAYLOAD[:1000])

    resumed = system.http_download(address + "/ranged", {"Range": "bytes=1000-"}, target, append=True)
    assert resumed.resumed and resumed.status == 206 and target.read_bytes() == PAYLOAD

    target.write_bytes(b"stale")
    replaced = system.http_download(address + "/data", {"Range": "bytes=5-"}, target, append=True)
    assert not replaced.resumed and target.read_bytes() == PAYLOAD


def test_http_download_of_a_redirect_or_error_writes_nothing(transport: tuple[System, str], tmp_path: Path) -> None:
    """A 3xx or 404 answer leaves the destination absent and is reported with its status."""
    system, address = transport
    target = tmp_path / "out.bin"

    moved = system.http_download(address + "/redirect", None, target)
    missing = system.http_download(address + "/missing", None, target)

    assert (moved.status, missing.status) == (302, 404) and not target.exists()


@pytest.mark.parametrize("address", ["ftp://example.org/x", "gopher://example.org/", "data:text/plain,hi"])
def test_only_http_https_and_file_urls_are_fetched_by_the_real_transport(address: str) -> None:
    """Any other scheme is refused before a connection is attempted."""
    with pytest.raises(OSError, match="Only http, https and file"):
        WindowsSystem().http_request(address)


def test_a_file_url_is_served_like_a_response(tmp_path: Path) -> None:
    """`file://` addresses return the file's bytes with status 200."""
    path = tmp_path / "local.txt"
    path.write_text("local data", encoding="utf-8")

    response = WindowsSystem().http_request(path.as_uri())

    assert response.status == 200 and response.body == b"local data"


def test_the_shared_client_runs_end_to_end_over_the_real_transport(server: ThreadingHTTPServer, tmp_path: Path) -> None:
    """Redirect following, the User-Agent, the file name and the digest cache work over real sockets."""
    client = HttpClient(WindowsSystem(), env={}, sleep=lambda seconds: None, cache_dir=tmp_path / "cache")

    text = client.get(base(server) + "/redirect")
    first = client.download(base(server) + "/redirect")
    with pytest.raises(HttpError, match="HTTP 404"):
        client.text(base(server) + "/missing")

    assert text.url.endswith("/data") and len(text.body) == len(PAYLOAD)
    assert first.filename == "payload.bin" and first.path.read_bytes() == PAYLOAD
    assert server.seen[0][2]["User-Agent"].startswith("gupkg/")
