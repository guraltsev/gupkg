"""Cover the download under the checksum policy: ``fetch_artifact``, ``fetch_git`` and ``reverify``.

Behaviour covered: the matrix of hash present, missing and wrong, with and without a skip, over
``https://`` and ``http://``, ending in a verified file, a skipped file with the right receipt
record, or a refusal that started no request; a mismatch that no flag can waive; every redirect
hop of a skipped download being https on its parsed form; the observed hash and final URL of
the receipt; a file that changes after it was verified; real streamed downloads from a loopback
server (a modified body, a cut-short body, a server that must never be contacted); a git source
pinned, mismatching and unpinned; and the proof that no setting or environment variable can
reach the rule.

Real: ``HttpClient``, the policy, the cache files and, in the loopback tests, the Windows
transport against a thread on 127.0.0.1. Mocked: ``FakeSystem`` serves the https fixtures and
plays ``git``. Out of scope: the engine and the CLI flag (issues 028 and 029), the manager.
"""

from __future__ import annotations

import ast
import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gupkg.model.checksum import (
    SKIPPED_CLI,
    SKIPPED_SETTING,
    USER_APPROVED,
    VERIFIED,
    ChecksumInvalid,
    ChecksumMismatch,
    ChecksumMissing,
    ChecksumRecord,
    check_before_download,
    git_gate,
    git_record,
    verify_observed,
)
from gupkg.model.settings import LAYER_APP, LAYER_ROOT, Layer, default_layer, merge_layers
from gupkg.report.events import Warning, use_reporter
from gupkg.services.artifact import FetchedArtifact, fetch_artifact, fetch_git, reverify
from gupkg.services.http import Download, DownloadMismatch as HttpMismatch, HttpClient, HttpError
from gupkg.system.fake import FakeHttp, FakeSystem
from gupkg.system.windows import WindowsSystem
from tests.test_git_services import COMMIT, OTHER, FakeGit, machine

BODY = bytes(range(256)) * 700
GOOD = hashlib.sha256(BODY).hexdigest()
WRONG = hashlib.sha256(b"something else").hexdigest()
HTTPS = "https://example.org/tool.zip"
HTTP = "http://example.org/tool.zip"


class Collect:
    """A reporter that keeps every event."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[object] = []

    def handle(self, event: object) -> None:
        """Keep one event."""
        self.events.append(event)


def make_client(system: FakeSystem, tmp_path: Path, **options) -> HttpClient:
    """Return a client over *system* that never sleeps and never retries unless told to."""
    options.setdefault("retries", 0)
    return HttpClient(system, env={}, sleep=lambda seconds: None, cache_dir=tmp_path / "cache", **options)


def cached_files(tmp_path: Path) -> list[str]:
    """Return the names of finished files in the download cache."""
    cache = tmp_path / "cache"
    return sorted(path.name for path in cache.glob("*.file")) if cache.is_dir() else []


def serve_both(system: FakeSystem, body: bytes = BODY) -> None:
    """Serve *body* at the https and the http address."""
    system.urls[HTTPS] = FakeHttp(body)
    system.urls[HTTP] = FakeHttp(body)


# ---------------------------------------------------------------------------
# The behaviour matrix
# ---------------------------------------------------------------------------

MATRIX = [
    pytest.param("present", None, HTTPS, "verified", id="hash-present-https-no-flag-verified"),
    pytest.param("present", SKIPPED_CLI, HTTPS, "verified", id="hash-present-https-flag-still-verified"),
    pytest.param("present", None, HTTP, "verified", id="hash-present-http-no-flag-verified"),
    pytest.param("present", SKIPPED_CLI, HTTP, "verified", id="hash-present-http-flag-still-verified"),
    pytest.param("missing", None, HTTPS, "missing-skippable", id="hash-missing-https-no-flag-refused"),
    pytest.param("missing", SKIPPED_CLI, HTTPS, SKIPPED_CLI, id="hash-missing-https-flag-skipped-cli"),
    pytest.param("missing", SKIPPED_SETTING, HTTPS, SKIPPED_SETTING, id="hash-missing-https-manager-setting-skipped-setting"),
    pytest.param("missing", USER_APPROVED, HTTPS, USER_APPROVED, id="hash-missing-https-manager-ask-user-approved"),
    pytest.param("missing", None, HTTP, "missing-unskippable", id="hash-missing-http-no-flag-refused"),
    pytest.param("missing", SKIPPED_CLI, HTTP, "missing-unskippable", id="hash-missing-http-flag-still-refused"),
    pytest.param("wrong", None, HTTPS, "mismatch", id="hash-wrong-https-no-flag-mismatch"),
    pytest.param("wrong", SKIPPED_CLI, HTTPS, "mismatch", id="hash-wrong-https-flag-still-mismatch"),
    pytest.param("wrong", None, HTTP, "mismatch", id="hash-wrong-http-no-flag-mismatch"),
    pytest.param("wrong", SKIPPED_CLI, HTTP, "mismatch", id="hash-wrong-http-flag-still-mismatch"),
]


@pytest.mark.parametrize(("hash_state", "skip", "url", "outcome"), MATRIX)
def test_the_policy_matrix_over_fetch_artifact(
    system: FakeSystem, tmp_path: Path, hash_state: str, skip: str | None, url: str, outcome: str
) -> None:
    """Each hash state, flag and scheme ends as the policy table says; refusals never touch the network."""
    serve_both(system)
    expected = {"present": GOOD, "missing": None, "wrong": WRONG}[hash_state]
    client = make_client(system, tmp_path)

    if outcome in ("missing-skippable", "missing-unskippable"):
        with pytest.raises(ChecksumMissing) as raised:
            fetch_artifact(client, url, expected_sha256=expected, skip=skip)
        assert raised.value.skippable is (outcome == "missing-skippable")
        assert raised.value.exit_code == 3 and raised.value.code == "checksum-missing"
        assert ("--skip-checksum" in str(raised.value)) is True
        assert system.request_log == [], "the download must never be started"
        assert cached_files(tmp_path) == []
    elif outcome == "mismatch":
        with pytest.raises(ChecksumMismatch) as raised:
            fetch_artifact(client, url, expected_sha256=expected, skip=skip)
        assert raised.value.exit_code == 3 and raised.value.code == "checksum-mismatch"
        assert cached_files(tmp_path) == [], "a file that failed verification must not stay in the cache"
    else:
        fetched = fetch_artifact(client, url, expected_sha256=expected, skip=skip)
        assert fetched.record.decision == outcome
        assert fetched.record.observed_sha256 == GOOD
        assert fetched.record.size == len(BODY) and fetched.record.url == url
        assert fetched.download.path.read_bytes() == BODY


def test_a_wrong_hash_is_a_mismatch_even_when_the_declared_digest_is_spelled_in_another_case(system: FakeSystem, tmp_path: Path) -> None:
    """Upper case and a `sha256:` prefix are the same digest; a different digest stays a mismatch."""
    serve_both(system)
    client = make_client(system, tmp_path)

    fetched = fetch_artifact(client, HTTPS, expected_sha256=" SHA256:" + GOOD.upper() + "\n")
    with pytest.raises(ChecksumMismatch):
        fetch_artifact(client, HTTPS, expected_sha256="sha256:" + WRONG.upper(), skip=SKIPPED_CLI)

    assert fetched.record.decision == VERIFIED and fetched.record.observed_sha256 == GOOD


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param(GOOD[:63], id="one-digit-short"),
        pytest.param(GOOD + "0", id="one-digit-long"),
        pytest.param("g" + GOOD[1:], id="non-hex-digit"),
        pytest.param(GOOD + " " + GOOD, id="two-digests"),
        pytest.param("", id="empty"),
        pytest.param(GOOD[:32] + "\n" + GOOD[32:], id="split-by-a-line-break"),
    ],
)
def test_a_malformed_declared_digest_is_refused_not_treated_as_missing_or_truncated(
    system: FakeSystem, tmp_path: Path, declared: str
) -> None:
    """A digest that is not exactly 64 hex digits is an error; the flag does not turn it into a skip."""
    serve_both(system)
    client = make_client(system, tmp_path)

    with pytest.raises(ChecksumInvalid):
        fetch_artifact(client, HTTPS, expected_sha256=declared, skip=SKIPPED_CLI)

    assert system.request_log == []


def test_an_unknown_skip_decision_is_refused(system: FakeSystem, tmp_path: Path) -> None:
    """Only the three skip decisions are accepted; anything else fails closed before a request."""
    serve_both(system)

    with pytest.raises(ChecksumInvalid):
        fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=None, skip="verified")

    assert system.request_log == []


# ---------------------------------------------------------------------------
# Warnings, receipts, redirects
# ---------------------------------------------------------------------------


def test_a_skipped_download_warns_and_a_verified_one_does_not(system: FakeSystem, tmp_path: Path) -> None:
    """The run shows the skip as a warning event; a verified download shows an ok step instead."""
    serve_both(system)
    reporter = Collect()

    with use_reporter(reporter):
        fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=None, skip=SKIPPED_CLI)
        skipped = [event for event in reporter.events if isinstance(event, Warning)]
        fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=GOOD)

    assert len(skipped) == 1 and "not verified" in skipped[0].text and SKIPPED_CLI in skipped[0].text
    assert len([event for event in reporter.events if isinstance(event, Warning)]) == 1


def test_the_record_holds_the_observed_hash_the_url_the_final_url_and_the_size(system: FakeSystem, tmp_path: Path) -> None:
    """After redirects the record keeps the asked-for URL and the one that served the bytes."""
    system.urls[HTTPS] = FakeHttp("", 302, {"Location": "https://cdn.example.net/blob/tool.zip"})
    system.urls["https://cdn.example.net/blob/tool.zip"] = FakeHttp(BODY)

    fetched = fetch_artifact(
        make_client(system, tmp_path), HTTPS, expected_sha256=GOOD, sha256_from="github-digest"
    )

    assert fetched.record.to_data() == {
        "decision": "verified", "url": HTTPS, "final_url": "https://cdn.example.net/blob/tool.zip",
        "observed_sha256": GOOD, "size": len(BODY), "sha256_from": "github-digest",
    }


def test_a_skipped_download_records_no_provenance_because_it_had_no_expected_hash(system: FakeSystem, tmp_path: Path) -> None:
    """`sha256_from` belongs to a declared hash; a skip has none, so the record leaves it out."""
    serve_both(system)

    fetched = fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=None, skip=SKIPPED_CLI, sha256_from="manual")

    assert "sha256_from" not in fetched.record.to_data()


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("http://cdn.example.net/blob", id="redirect-to-plain-http"),
        pytest.param("https://good.example@evil.example/blob", id="redirect-with-user-information"),
        pytest.param("https:/cdn.example.net/blob", id="redirect-with-one-slash"),
    ],
)
def test_every_hop_of_a_skipped_download_must_be_https(system: FakeSystem, tmp_path: Path, target: str) -> None:
    """A redirect to http, to an address with user information or to an odd spelling stops a skipped download."""
    system.urls[HTTPS] = FakeHttp("", 302, {"Location": target})
    system.urls[target] = FakeHttp(BODY)

    with pytest.raises(HttpError):
        fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=None, skip=SKIPPED_CLI)

    assert target not in [request.url for request in system.request_log]
    assert cached_files(tmp_path) == []


def test_the_client_itself_refuses_a_first_address_that_is_not_https_when_asked_to(system: FakeSystem, tmp_path: Path) -> None:
    """`download(require_https=True)` stops before the first request, whatever the caller checked."""
    serve_both(system)

    with pytest.raises(HttpError, match="not https"):
        make_client(system, tmp_path).download(HTTP, require_https=True)

    assert system.request_log == []


def test_the_same_userinfo_redirect_is_followed_when_the_hash_is_declared(system: FakeSystem, tmp_path: Path) -> None:
    """The https-only rule belongs to the skip: a verified download follows an ordinary https redirect."""
    target = "https://good.example@evil.example/blob"
    system.urls[HTTPS] = FakeHttp("", 302, {"Location": target})
    system.urls[target] = FakeHttp(BODY)

    fetched = fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=GOOD)

    assert fetched.record.final_url == target


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("HTTPS://example.org/tool.zip", id="upper-case-scheme"),
        pytest.param("https://example.org:8443/tool.zip", id="explicit-port"),
    ],
)
def test_https_is_recognised_in_its_ordinary_spellings(system: FakeSystem, tmp_path: Path, url: str) -> None:
    """A legitimate https address in capital letters, or with a port, can be skipped."""
    system.urls[url] = FakeHttp(BODY)

    fetched = fetch_artifact(make_client(system, tmp_path), url, expected_sha256=None, skip=SKIPPED_CLI)

    assert fetched.record.decision == SKIPPED_CLI


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("file:///C:/share/tool.zip", id="file-url"),
        pytest.param("file://server/share/tool.zip", id="file-url-with-host"),
        pytest.param("\\\\?\\C:\\share\\tool.zip", id="extended-length-path"),
        pytest.param("https:/example.org/tool.zip", id="one-slash"),
        pytest.param("https:\\\\example.org\\tool.zip", id="backslashes"),
        pytest.param("https://good.example@evil.example/tool.zip", id="user-information"),
        pytest.param("https://", id="no-host"),
        pytest.param("https://example.org/a b.zip", id="space"),
        pytest.param("ftp://example.org/tool.zip", id="ftp"),
    ],
)
def test_an_address_that_is_not_plainly_https_cannot_be_skipped(system: FakeSystem, tmp_path: Path, url: str) -> None:
    """Odd spellings and other schemes are refused with the flag, before any request is made."""
    with pytest.raises(ChecksumMissing) as raised:
        fetch_artifact(make_client(system, tmp_path), url, expected_sha256=None, skip=SKIPPED_CLI)

    assert raised.value.skippable is False
    assert system.request_log == []


def test_a_file_url_with_a_declared_hash_follows_the_ordinary_rules(system: FakeSystem, tmp_path: Path) -> None:
    """A local `file:` artifact is verified like any other; without a hash it is refused even with the flag."""
    share = tmp_path / "share"
    share.mkdir()
    (share / "tool.zip").write_bytes(BODY)
    url = (share / "tool.zip").as_uri()
    system.urls[url] = FakeHttp(BODY)

    fetched = fetch_artifact(make_client(system, tmp_path), url, expected_sha256=GOOD)

    assert fetched.record.decision == VERIFIED
    with pytest.raises(ChecksumMissing):
        fetch_artifact(make_client(system, tmp_path), url, expected_sha256=None, skip=SKIPPED_CLI)


def test_a_file_that_changes_after_it_was_verified_is_caught_before_use(system: FakeSystem, tmp_path: Path) -> None:
    """`reverify` re-hashes the cached file; a tampered or vanished file is a mismatch."""
    serve_both(system)
    fetched = fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=GOOD)
    reverify(fetched)

    with fetched.download.path.open("r+b") as handle:
        handle.write(b"\xff")  # the body starts with 0x00, so this changes it
    with pytest.raises(ChecksumMismatch):
        reverify(fetched)
    fetched.download.path.unlink()
    with pytest.raises(ChecksumMismatch):
        reverify(fetched)


def test_a_cached_file_that_no_longer_matches_is_not_reused(system: FakeSystem, tmp_path: Path) -> None:
    """A cached copy modified on disk is discarded and downloaded again, then verified."""
    serve_both(system)
    first = fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=GOOD)
    first.download.path.write_bytes(b"planted")

    again = fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=GOOD)

    assert again.download.path.read_bytes() == BODY and again.download.from_cache is False


def test_a_download_without_a_hash_never_uses_the_cache(system: FakeSystem, tmp_path: Path) -> None:
    """A skipped download fetches again each time, so an old cached file can never stand in for it."""
    serve_both(system)
    client = make_client(system, tmp_path)
    fetch_artifact(client, HTTPS, expected_sha256=GOOD)
    system.request_log.clear()

    fetched = fetch_artifact(client, HTTPS, expected_sha256=None, skip=SKIPPED_CLI)

    assert len(system.request_log) == 1 and fetched.download.from_cache is False


# ---------------------------------------------------------------------------
# Truncated and interrupted bodies on the fake boundary
# ---------------------------------------------------------------------------


def test_an_interrupted_download_fails_and_leaves_no_ready_file(system: FakeSystem, tmp_path: Path) -> None:
    """A connection dropped half-way is an HttpError after the retries, and nothing verified is cached."""
    system.urls[HTTPS] = FakeHttp(BODY, fail_after=1000)

    with pytest.raises(HttpError):
        fetch_artifact(make_client(system, tmp_path, retries=1), HTTPS, expected_sha256=GOOD)

    assert cached_files(tmp_path) == []


# ---------------------------------------------------------------------------
# Real streamed downloads from a loopback server
# ---------------------------------------------------------------------------


class Loopback(BaseHTTPRequestHandler):
    """Serve BODY, a modified body, and a cut-short body; count the requests."""

    hits: list[str] = []

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature of the base class
        """Keep the test output clean."""

    def do_GET(self) -> None:  # noqa: N802 - the name is the HTTP method
        """Answer by path."""
        type(self).hits.append(self.path)
        if self.path == "/good":
            body = BODY
        elif self.path == "/modified":
            body = BODY[:-1] + bytes([BODY[-1] ^ 1])
        elif self.path == "/short":
            self.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n" % len(BODY) + BODY[:1000])
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def loopback() -> Iterator[tuple[str, list[str]]]:
    """A loopback server; yields its base address and the list of paths it was asked for."""
    Loopback.hits = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Loopback)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", Loopback.hits
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def real_client(tmp_path: Path) -> HttpClient:
    """Return a client over the real Windows transport."""
    return HttpClient(WindowsSystem(), env={}, sleep=lambda seconds: None, cache_dir=tmp_path / "cache", retries=0)


def test_a_real_streamed_download_is_hashed_and_recorded(loopback: tuple[str, list[str]], tmp_path: Path) -> None:
    """The bytes that came over a socket are the bytes whose hash is recorded as observed."""
    base, hits = loopback

    fetched = fetch_artifact(real_client(tmp_path), base + "/good", expected_sha256=GOOD)

    assert fetched.record.observed_sha256 == hashlib.sha256(fetched.download.path.read_bytes()).hexdigest() == GOOD
    assert fetched.record.size == len(BODY) and hits == ["/good"]


def test_a_real_modified_body_is_a_mismatch_and_is_not_left_in_the_cache(loopback: tuple[str, list[str]], tmp_path: Path) -> None:
    """One flipped bit in a streamed body fails verification, flag or not, and no ready file remains."""
    base, _ = loopback

    with pytest.raises(ChecksumMismatch) as raised:
        fetch_artifact(real_client(tmp_path), base + "/modified", expected_sha256=GOOD, skip=SKIPPED_CLI)

    assert GOOD in str(raised.value) and cached_files(tmp_path) == []


def test_a_real_body_cut_short_is_an_error_not_a_hash_of_a_prefix(loopback: tuple[str, list[str]], tmp_path: Path) -> None:
    """A body that ends before its Content-Length fails; a prefix is never accepted or recorded."""
    base, _ = loopback

    with pytest.raises(HttpError):
        fetch_artifact(real_client(tmp_path), base + "/short", expected_sha256=GOOD)

    assert cached_files(tmp_path) == []


def test_a_missing_hash_over_plain_http_never_connects(loopback: tuple[str, list[str]], tmp_path: Path) -> None:
    """With the flag and an http address the server is not contacted at all."""
    base, hits = loopback

    with pytest.raises(ChecksumMissing):
        fetch_artifact(real_client(tmp_path), base + "/good", expected_sha256=None, skip=SKIPPED_CLI)

    assert hits == []


# ---------------------------------------------------------------------------
# Nothing but the call decides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", [LAYER_ROOT, LAYER_APP], ids=["root-layer", "app-layer"])
def test_a_checksum_missing_skip_setting_has_no_effect_on_the_download_rule(
    system: FakeSystem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layer: str
) -> None:
    """`[manager] checksum_missing = "skip"` in a settings layer, and an environment variable, change nothing."""
    settings = merge_layers([default_layer("user"), Layer(layer, {"schema": 4, "manager": {"checksum_missing": "skip"}})])
    assert settings["manager.checksum_missing"] == "skip"
    for name in ("GUPKG_SKIP_CHECKSUM", "GUPKG_CHECKSUM_MISSING", "SKIP_CHECKSUM"):
        monkeypatch.setenv(name, "skip")
    serve_both(system)

    with pytest.raises(ChecksumMissing):
        fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=None)

    assert system.request_log == []


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "gupkg"
# The roots of the download rule: the gate, the fetch, the client it downloads through, and the git checkout.
RULE_ROOTS = ("gupkg.model.checksum", "gupkg.services.artifact", "gupkg.services.http", "gupkg.services." + "unpack.git" + "source")
# Modules that read the environment, and why that cannot weaken a check: they only detect an elevated run
# (``GUPKG_ELEVATED`` and the process token), and an elevated run is stricter.
ENVIRONMENT_READERS = {"gupkg.system.port", "gupkg.selfmgmt.elevation"}
FORBIDDEN_ATTRIBUTES = {"environ", "environb", "getenv", "argv", "import_module"}
FORBIDDEN_NAMES = {"__import__", "getenv"}


def module_file(name: str) -> Path | None:
    """Return the source file of a gupkg module, or ``None`` for anything else."""
    base = SOURCE_ROOT.joinpath(*name.split(".")[1:])
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def gupkg_imports(name: str, path: Path) -> set[str]:
    """Return the gupkg modules that the module *name* imports (relative imports resolved)."""
    package = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            base = ".".join(package[: len(package) - (node.level - 1)] + ([node.module] if node.module else [])) if node.level else node.module or ""
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
    return {item for item in found if item.startswith("gupkg") and module_file(item) is not None}


def rule_closure() -> dict[str, Path]:
    """Return every gupkg module that the download rule imports, directly or through other modules.

    The parent packages of a module count too: importing ``gupkg.services.http`` runs ``gupkg/__init__.py`` and
    ``gupkg/services/__init__.py`` first.
    """
    seen: dict[str, Path] = {}
    pending = list(RULE_ROOTS)
    while pending:
        name = pending.pop()
        path = module_file(name)
        if name not in seen and path is not None:
            seen[name] = path
            pending += gupkg_imports(name, path)
            parts = name.split(".")
            pending += [".".join(parts[:count]) for count in range(1, len(parts))]
    return seen


def reads_the_environment(tree: ast.AST) -> bool:
    """Return whether a syntax tree reads the environment, however it is spelled.

    ``os.environ``, ``os.getenv``, ``sys.argv``, ``__import__``, ``importlib.import_module``, and the same names
    brought in with ``from os import environ``, ``from sys import argv as arguments`` and the like.
    """
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module in ("os", "sys", "importlib")
        for alias in node.names
    }
    return bool((FORBIDDEN_ATTRIBUTES & attributes) or (FORBIDDEN_NAMES & names) or (FORBIDDEN_ATTRIBUTES & imported))


def test_the_download_rule_and_everything_it_imports_read_neither_settings_nor_the_environment() -> None:
    """Across the transitive imports of the rule, only the two elevation detectors touch the environment; none imports settings.

    Flags `os.environ`, `os.environb`, `os.getenv`, `sys.argv`, `__import__` and `importlib.import_module`.
    """
    closure = rule_closure()
    assert {"gupkg.model.checksum", "gupkg.services.http", "gupkg.system.port"} <= set(closure), "the closure was not computed"
    readers: set[str] = set()
    for name, path in closure.items():
        if reads_the_environment(ast.parse(path.read_text(encoding="utf-8"))):
            readers.add(name)
        assert "settings" not in name and "manager" not in name, f"{name} is part of the download rule's imports"
    assert readers == ENVIRONMENT_READERS, f"unexpected readers of the environment: {sorted(readers ^ ENVIRONMENT_READERS)}"
    for name in ("gupkg.model.checksum", "gupkg.services.artifact"):
        tree = ast.parse(closure[name].read_text(encoding="utf-8"))
        direct = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert "os" not in direct and "sys" not in direct, name


def test_the_forbidden_name_check_is_not_vacuous() -> None:
    """The checks used above do flag each spelling when it is present."""
    spellings = ["import os\nos.environ", "import os\nos.getenv('x')", "import os\nos.environb", "import sys\nsys.argv",
                 "__import__('os')", "import importlib\nimportlib.import_module('os')",
                 "from os import environ as variables", "from sys import argv", "from os import getenv",
                 "from importlib import import_module"]
    for text in spellings:
        assert reads_the_environment(ast.parse(text)), text
    assert not reads_the_environment(ast.parse("import os\nos.pathsep\nfrom os import path"))


def test_the_closure_includes_the_parent_packages_of_every_module() -> None:
    """Importing a module runs its parent packages' ``__init__.py`` first, so they belong to the closure."""
    assert {"gupkg", "gupkg.services", "gupkg.model"} <= set(rule_closure())


# ---------------------------------------------------------------------------
# Git sources
# ---------------------------------------------------------------------------

GIT_URL = "https://example.org/team/tool.git"


def checkout(tmp_path: Path, git: FakeGit, *, commit: str | None, skip: str | None = None, url: str = GIT_URL):
    """Run `fetch_git` on a machine whose only process is *git*."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return fetch_git(machine(tmp_path, git), url, "refs/heads/main", commit, tmp_path / "app", work, skip=skip)


def test_a_pinned_commit_is_verified_and_recorded(tmp_path: Path) -> None:
    """A source that pins a commit is anchored by it, and the receipt records the commit."""
    record = checkout(tmp_path, FakeGit(), commit=COMMIT)

    assert record.decision == VERIFIED and record.commit == COMMIT and record.url == GIT_URL


@pytest.mark.parametrize("skip", [None, SKIPPED_CLI], ids=["no-flag", "flag"])
def test_a_pinned_commit_that_the_repository_does_not_serve_is_a_mismatch(tmp_path: Path, skip: str | None) -> None:
    """A moved branch or a forged repository cannot change what a pinned version installs; no flag waives it."""
    with pytest.raises(ChecksumMismatch) as raised:
        checkout(tmp_path, FakeGit(served=OTHER), commit=COMMIT, skip=skip)

    assert raised.value.expected == COMMIT and raised.value.observed == OTHER
    assert not (tmp_path / "app").exists()


def test_a_ref_without_a_commit_is_refused_before_git_runs(tmp_path: Path) -> None:
    """With nothing pinned and no flag, git is never started."""
    git = FakeGit()

    with pytest.raises(ChecksumMissing) as raised:
        checkout(tmp_path, git, commit=None)

    assert raised.value.skippable is True and git.calls == []


def test_a_ref_without_a_commit_may_be_skipped_over_https_and_records_the_resolved_commit(tmp_path: Path) -> None:
    """The flag lets an unpinned https repository through, and the receipt still records the commit that was resolved."""
    record = checkout(tmp_path, FakeGit(), commit=None, skip=SKIPPED_CLI)

    assert record.decision == SKIPPED_CLI and record.commit == COMMIT


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("ssh://git@example.org/team/tool.git", id="ssh"),
        pytest.param("git://example.org/team/tool.git", id="git-protocol"),
        pytest.param("git@example.org:team/tool.git", id="scp-style"),
        pytest.param("http://example.org/team/tool.git", id="http"),
    ],
)
def test_an_unpinned_repository_that_is_not_https_cannot_be_skipped(tmp_path: Path, url: str) -> None:
    """The https-only rule holds for git: the flag does not help ssh, git, scp-style or http addresses."""
    git = FakeGit()

    with pytest.raises(ChecksumMissing) as raised:
        checkout(tmp_path, git, commit=None, skip=SKIPPED_CLI, url=url)

    assert raised.value.skippable is False and git.calls == []


def test_fetch_result_type_is_frozen(system: FakeSystem, tmp_path: Path) -> None:
    """A fetched artifact is immutable data, so a later step cannot swap its record."""
    serve_both(system)
    fetched = fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=GOOD)

    assert isinstance(fetched, FetchedArtifact)
    with pytest.raises(AttributeError):
        fetched.record = None  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Near misses: digests and commits that differ only late, or only early
# ---------------------------------------------------------------------------

POSITIONS = [
    pytest.param(0, id="first-character"),
    pytest.param(31, id="last-of-the-first-half"),
    pytest.param(32, id="first-of-the-second-half"),
    pytest.param(40, id="middle-of-the-second-half"),
    pytest.param(62, id="second-to-last"),
    pytest.param(63, id="last-character"),
]


def flip(digest: str, position: int) -> str:
    """Return *digest* with the one character at *position* changed."""
    return digest[:position] + ("0" if digest[position] != "0" else "1") + digest[position + 1:]


@pytest.mark.parametrize("position", POSITIONS)
def test_verify_observed_compares_every_digit(position: int) -> None:
    """A digest that differs from the expected one in a single digit, wherever it is, is a mismatch, in both directions."""
    expected = "a" * 64
    observed = flip(expected, position)

    for left, right in ((expected, observed), (observed, expected)):
        with pytest.raises(ChecksumMismatch):
            verify_observed(check_before_download(HTTPS, left), right, url=HTTPS, final_url=HTTPS, size=1)


@pytest.mark.parametrize("position", POSITIONS)
def test_a_download_whose_hash_differs_from_the_declared_one_in_one_digit_is_a_mismatch(
    system: FakeSystem, tmp_path: Path, position: int
) -> None:
    """The client and the policy compare all 64 digits of a freshly downloaded file."""
    serve_both(system)

    with pytest.raises(ChecksumMismatch):
        fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=flip(GOOD, position))

    assert cached_files(tmp_path) == []


@pytest.mark.parametrize("position", POSITIONS)
def test_a_cached_file_is_reused_only_for_the_exact_digest(system: FakeSystem, tmp_path: Path, position: int) -> None:
    """A cache entry whose real digest differs from the expected one in a single digit is not a hit.

    The entry is planted under the key of the near-miss digest; the server now serves other bytes, so the only
    way to succeed would be to accept the planted file.
    """
    near = flip(GOOD, position)
    cache = tmp_path / "cache"
    cache.mkdir()
    key = hashlib.sha256(f"{HTTPS}\n{near}".encode()).hexdigest()[:40]
    (cache / f"{key}.file").write_bytes(BODY)
    system.urls[HTTPS] = FakeHttp(b"the server serves something else now")

    with pytest.raises(ChecksumMismatch):
        fetch_artifact(make_client(system, tmp_path), HTTPS, expected_sha256=near)
    with pytest.raises(HttpMismatch):
        make_client(system, tmp_path).download(HTTPS, expected_sha256=near)

    assert system.request_log, "the planted file must not have been accepted without a request"


@pytest.mark.parametrize("position", POSITIONS)
def test_reverify_compares_every_digit(tmp_path: Path, position: int) -> None:
    """A record whose observed digest differs from the file's in one digit, wherever, fails the re-check."""
    path = tmp_path / "tool.zip"
    path.write_bytes(BODY)
    download = Download(path, len(BODY), GOOD, HTTPS, "tool.zip")
    fetched = FetchedArtifact(download, ChecksumRecord(VERIFIED, url=HTTPS, final_url=HTTPS, observed_sha256=flip(GOOD, position), size=len(BODY)))

    with pytest.raises(ChecksumMismatch):
        reverify(fetched)
    reverify(FetchedArtifact(download, ChecksumRecord(VERIFIED, url=HTTPS, final_url=HTTPS, observed_sha256=GOOD, size=len(BODY))))


@pytest.mark.parametrize("position", [pytest.param(0, id="first"), pytest.param(19, id="twentieth"), pytest.param(20, id="twenty-first"), pytest.param(38, id="second-to-last"), pytest.param(39, id="last")])
def test_a_commit_that_differs_in_one_digit_is_a_mismatch(tmp_path: Path, position: int) -> None:
    """`git_record` and the checkout compare all 40 digits of the pinned commit."""
    served = flip(COMMIT, position)

    with pytest.raises(ChecksumMismatch):
        git_record(git_gate(GIT_URL, COMMIT), served, url=GIT_URL)
    with pytest.raises(ChecksumMismatch):
        checkout(tmp_path, FakeGit(served=served), commit=COMMIT)

    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize("final_url", [None, 5, b"https://example.org/x"], ids=["none", "integer", "bytes"])
def test_a_skipped_download_with_a_final_address_that_is_not_text_fails_closed(final_url: object) -> None:
    """A client that reports a non-text final URL gets a checksum error with exit code 3, never a TypeError."""
    verification = check_before_download(HTTPS, None, SKIPPED_CLI)

    with pytest.raises(ChecksumMismatch) as raised:
        verify_observed(verification, GOOD, url=HTTPS, final_url=final_url, size=1)  # type: ignore[arg-type]

    assert raised.value.exit_code == 3
