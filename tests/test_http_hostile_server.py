"""Cover the real transport against a hostile loopback server: dripped headers, cut-short bodies, encodings.

A thread on 127.0.0.1 answers like a misbehaving server. Covers the total deadline while the *headers*
arrive (an idle socket timeout alone never fires when a line arrives every tenth of a second), the
protocol errors of ``http.client`` becoming ordinary ``OSError`` and ``HttpError``, a body that ends before
its ``Content-Length``, and a ``Content-Encoding`` that gupkg refuses to decompress. Nothing leaves the
machine. Out of scope: well-behaved servers (``test_http_transport``).
"""

from __future__ import annotations

import gzip
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gupkg.services.http import HttpClient, HttpError
from gupkg.system.windows import WindowsSystem


class Hostile(BaseHTTPRequestHandler):
    """Write raw, misbehaving answers for the routes below."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature of the base class
        """Keep the test output clean."""

    def send(self, data: bytes, pause: float = 0.0) -> None:
        """Write and flush *data*, optionally pausing after it; a client that left is not an error."""
        try:
            self.wfile.write(data)
            self.wfile.flush()
            time.sleep(pause)
        except OSError:
            raise ConnectionAbortedError from None

    def do_GET(self) -> None:  # noqa: N802 - the name is the HTTP method
        """Answer GET according to the path."""
        try:
            if self.path == "/header-drip":
                self.send(b"HTTP/1.0 200 OK\r\n")
                for index in range(90):
                    self.send(f"X-Drip-{index}: v\r\n".encode(), 0.1)
                self.send(b"\r\nbody")
            elif self.path == "/many-headers":
                self.send(b"HTTP/1.0 200 OK\r\n" + b"".join(f"X-{i}: v\r\n".encode() for i in range(150)) + b"\r\nbody")
            elif self.path == "/short":
                self.send(b"HTTP/1.0 200 OK\r\nContent-Length: 5000\r\n\r\n0123456789")
            elif self.path == "/gzip":
                body = gzip.compress(b"hello hello hello")
                self.send(b"HTTP/1.0 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            elif self.path == "/exact":
                self.send(b"HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\nhello")
        except ConnectionAbortedError:
            pass


@pytest.fixture
def hostile() -> Iterator[str]:
    """A loopback server with misbehaving routes; yields its base address."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Hostile)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_the_total_deadline_ends_a_request_whose_headers_drip_in(hostile: str) -> None:
    """A header line every 0.1 s never trips the idle timeout; the deadline closes the connection instead."""
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        WindowsSystem().http_request(hostile + "/header-drip", timeout=5, total_timeout=0.8)

    assert time.monotonic() - started < 4


def test_the_client_maps_the_deadline_while_headers_arrive_to_an_http_error(hostile: str) -> None:
    """`HttpClient.get` fails with HttpError at the request deadline instead of running for the whole drip."""
    client = HttpClient(WindowsSystem(), env={}, sleep=lambda seconds: None, retries=0, request_deadline=1.0)
    started = time.monotonic()

    with pytest.raises(HttpError):
        client.get(hostile + "/header-drip")

    assert time.monotonic() - started < 5


def test_a_protocol_error_of_http_client_is_an_oserror_and_an_http_error(hostile: str) -> None:
    """More than 100 header lines raises `http.client.HTTPException` in the library; callers see OSError and HttpError."""
    with pytest.raises(OSError, match="HTTP protocol error"):
        WindowsSystem().http_request(hostile + "/many-headers")
    client = HttpClient(WindowsSystem(), env={}, sleep=lambda seconds: None, retries=1)

    with pytest.raises(HttpError, match="Could not read"):
        client.get(hostile + "/many-headers")


def test_a_body_that_ends_before_its_content_length_is_an_error_not_data(hostile: str, tmp_path: Path) -> None:
    """Ten bytes after `Content-Length: 5000` fail the request and the download; an exact length is fine."""
    system = WindowsSystem()

    with pytest.raises(OSError, match="cut short"):
        system.http_request(hostile + "/short")
    with pytest.raises(OSError, match="cut short"):
        system.http_download(hostile + "/short", None, tmp_path / "out.bin")
    assert system.http_request(hostile + "/exact").body == b"hello"


def test_a_compressed_response_is_refused(hostile: str, tmp_path: Path) -> None:
    """gupkg asks for identity and never decompresses: a `Content-Encoding: gzip` answer is an error."""
    system = WindowsSystem()

    with pytest.raises(OSError, match="Content-Encoding"):
        system.http_request(hostile + "/gzip")
    with pytest.raises(OSError, match="Content-Encoding"):
        system.http_download(hostile + "/gzip", None, tmp_path / "out.bin")
    assert not (tmp_path / "out.bin").exists()
