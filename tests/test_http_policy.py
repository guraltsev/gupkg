"""Cover the HTTP client's hardening: header names, secret headers on redirects, deadlines, token source, UNC URLs.

The client is real over ``FakeSystem``; the real transport's total deadline is checked against a
loopback server that trickles its answer. Out of scope: retries, redirects and downloads
(``test_http_client``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gupkg.services.http import HttpClient, HttpError, check_header_map, github_environment
from gupkg.system.fake import FakeHttp, FakeSystem
from gupkg.system.windows import WindowsSystem

URL = "https://example.org/data"


def make_client(system: FakeSystem, **options) -> HttpClient:
    """Return a client over *system* that never sleeps and ignores the process environment."""
    return HttpClient(system, env={}, sleep=lambda seconds: None, **options)


@pytest.mark.parametrize(
    "name",
    ["Host", "content-length", "Transfer-Encoding", "Connection", "Upgrade", "TE", "Proxy-Authorization", "Proxy-Connection",
     "Accept-Encoding", "Range", "Keep-Alive"],
)
def test_manifest_headers_cannot_set_framing_routing_or_hop_by_hop_names(name: str) -> None:
    """A manifest may not set `Host`, `Content-Length`, `Connection`, `Proxy-*` and the like: the transport owns them."""
    with pytest.raises(ValueError, match="set by gupkg's transport"):
        check_header_map({name: "x"})


def test_secret_looking_headers_are_dropped_on_a_cross_host_redirect_but_plain_ones_stay(system: FakeSystem) -> None:
    """`X-Api-Key`, `X-Auth-Token` and `X-Session-Secret` stay home on a redirect to another host; `Referer` follows."""
    system.urls["https://api.example.org/start"] = FakeHttp("", 302, {"Location": "https://cdn.example.net/blob"})
    system.urls["https://cdn.example.net/blob"] = FakeHttp("data")
    sent = {"X-Api-Key": "k", "X-Auth-Token": "t", "X-Session-Secret": "s", "Referer": "https://api.example.org/"}

    make_client(system).get("https://api.example.org/start", sent)

    first, second = (request.headers for request in system.request_log)
    assert {"X-Api-Key", "X-Auth-Token", "X-Session-Secret", "Referer"} <= set(first)
    assert set(second) & {"X-Api-Key", "X-Auth-Token", "X-Session-Secret"} == set()
    assert second["Referer"] == "https://api.example.org/"


def test_a_request_has_a_total_deadline_not_only_an_idle_timeout(system: FakeSystem) -> None:
    """A server that keeps answering 503 cannot keep a request alive forever: the total deadline ends it."""
    system.urls[URL] = FakeHttp("", 503)
    client = HttpClient(system, env={}, sleep=lambda seconds: time.sleep(0.05), retries=1000, backoff=0.05, request_deadline=0.4)

    with pytest.raises(HttpError, match="in total"):
        client.text(URL)

    assert 0 < system.request_log[0].total_timeout <= 0.4


def test_a_download_has_a_total_deadline(system: FakeSystem, tmp_path: Path) -> None:
    """The download deadline stops retries of a failing transfer and is passed to the transport as its total time."""
    system.urls[URL] = FakeHttp("", 503)
    client = HttpClient(system, env={}, sleep=lambda seconds: time.sleep(0.05), retries=1000, backoff=0.05,
                        cache_dir=tmp_path / "cache", download_deadline=0.4)

    with pytest.raises(HttpError, match="in total"):
        client.download(URL)

    assert system.request_log[0].total_timeout <= 0.4


def test_the_github_token_comes_from_an_explicit_mapping_and_is_dropped_in_an_elevated_run(
    system: FakeSystem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`github_environment` passes the token on for a normal run, and nothing for an elevated one or one marked elevated."""
    monkeypatch.delenv("GUPKG_ELEVATED", raising=False)
    environ = {"GITHUB_TOKEN": "t0ken", "OTHER": "x"}

    assert github_environment(system, environ) == {"GITHUB_TOKEN": "t0ken"}
    assert github_environment(FakeSystem(tmp_path / "admin", elevated=True), environ) == {}
    monkeypatch.setenv("GUPKG_ELEVATED", "1")
    assert github_environment(system, environ) == {}
    assert github_environment(system, {}) == {}


def test_a_client_never_reads_the_process_environment_for_the_token(system: FakeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit mapping the client sends no token, even when GITHUB_TOKEN is set in the process."""
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-process")
    system.urls["https://api.github.com/x"] = "{}"

    HttpClient(system, sleep=lambda seconds: None).text("https://api.github.com/x")

    assert "Authorization" not in system.request_log[0].headers


@pytest.mark.parametrize(
    "address",
    ["file:////server/share/tool.zip", "file://server/share/tool.zip", "file://evil.example/x"],
    ids=["unc-four-slashes", "host-form", "named-host"],
)
def test_file_urls_that_name_a_network_share_are_refused_unless_allowed(system: FakeSystem, tmp_path: Path, address: str) -> None:
    """Reading a UNC `file:` URL makes Windows authenticate to that server, so the client refuses it by default."""
    client = make_client(system, cache_dir=tmp_path / "cache")

    with pytest.raises(HttpError, match="names a network share"):
        client.text(address)
    with pytest.raises(HttpError, match="names a network share"):
        client.download(address)
    with pytest.raises(HttpError, match="Could not read"):
        make_client(system, allow_unc_file_urls=True).text(address)
    assert system.request_log == [] or all(request.url == address for request in system.request_log)


class Drip(BaseHTTPRequestHandler):
    """Answer with a body that arrives one byte at a time, far slower than any sane limit."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature of the base class
        """Keep the test output clean."""

    def do_GET(self) -> None:  # noqa: N802 - the name is the HTTP method
        """Send a 200 and then trickle the body."""
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()
        try:
            for _ in range(100):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass


@pytest.fixture
def drip_server() -> Iterator[str]:
    """A loopback server whose answers trickle; yields its base address."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_the_real_transport_enforces_the_total_time_against_a_trickling_server(drip_server: str, tmp_path: Path) -> None:
    """Each byte arrives inside the idle timeout, yet request and download stop at the total limit."""
    system = WindowsSystem()
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        system.http_request(drip_server + "/", timeout=5, total_timeout=0.6)
    with pytest.raises(TimeoutError):
        system.http_download(drip_server + "/", None, tmp_path / "out.bin", timeout=5, total_timeout=0.6)

    assert time.monotonic() - started < 5
