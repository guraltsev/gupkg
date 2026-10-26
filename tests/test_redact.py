"""Cover the removal of credentials from URLs in receipts, events, errors and plans.

Behaviour covered: user information and the values of secret-looking query parameters (`token`, `access_token`,
`X-Amz-Signature`, `sig`, `api_key`...) become `***`, the rest of the URL is untouched, a URL without secrets is returned as it was,
`redact_text` finds the URLs inside a sentence and keeps trailing punctuation; an install of a URL that carries a secret writes
a receipt, events, a plan and an error that never contain it.

Real: the redaction, the engine, real receipts. Mocked: `FakeSystem` (the URL fixtures).
Out of scope: how a front end prints events (issue 029).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gupkg.engine import lifecycle
from gupkg.model.plan import PackageResult, Request
from gupkg.redact import redact_text, redact_url
from gupkg.report.events import use_reporter
from tests.engine_support import World, pin_no_ambient_elevation, pkg_text, zip_bytes  # noqa: F401


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:hunter2@example.org/a.zip", "https://***@example.org/a.zip"),
        ("https://token@example.org/a.zip", "https://***@example.org/a.zip"),
        ("https://example.org/a.zip?token=SECRET", "https://example.org/a.zip?token=***"),
        ("https://example.org/a.zip?access_token=S&v=2", "https://example.org/a.zip?access_token=***&v=2"),
        ("https://s3.example/b/a.zip?X-Amz-Signature=abc&X-Amz-Credential=AKIA/x&X-Amz-Date=2026", "https://s3.example/b/a.zip?X-Amz-Signature=***&X-Amz-Credential=***&X-Amz-Date=2026"),
        ("https://example.org/a.zip?API_KEY=1&sig=2&Password=3&auth=4", "https://example.org/a.zip?API_KEY=***&sig=***&Password=***&auth=***"),
        ("https://u:p@example.org/a.zip?token=T#frag", "https://***@example.org/a.zip?token=***#frag"),
        ("https://example.org/a.zip#access_token=S&state=ok", "https://example.org/a.zip#access_token=***&state=ok"),
        ("https://example.org/a.zip?v=1;token=S;arch=x64", "https://example.org/a.zip?v=1;token=***;arch=x64"),
        ("https://cdn.example/a.zip?Policy=P&Signature=S&Key-Pair-Id=K", "https://cdn.example/a.zip?Policy=***&Signature=***&Key-Pair-Id=***"),
        ("https://b.s3.example/a.zip?AWSAccessKeyId=AKIA&Expires=1&X-Amz-Credential=C", "https://b.s3.example/a.zip?AWSAccessKeyId=***&Expires=1&X-Amz-Credential=***"),
        ("https://example.org/a.zip?sessionid=1&x-ms-session=2", "https://example.org/a.zip?sessionid=***&x-ms-session=***"),
        ("https://example.org/a.zip?a=1;sig=S", "https://example.org/a.zip?a=1;sig=***"),
        ("https://example.org/a.zip?a=1;b=2;token=S;c=3", "https://example.org/a.zip?a=1;b=2;token=***;c=3"),
        ("https://example.org/a.zip?%74oken=S", "https://example.org/a.zip?%74oken=***"),
        ("https://example.org/a.zip?tok%65n=S&v=1", "https://example.org/a.zip?tok%65n=***&v=1"),
        ("https://example.org/a.zip?token%3DS", "https://example.org/a.zip?token=***"),
        ("https://example.org/a.zip?%2574oken=S", "https://example.org/a.zip?%2574oken=***"),
        ("https://example.org/a.zip?pwd=S&passwd=S&Password=S&pass=S", "https://example.org/a.zip?pwd=***&passwd=***&Password=***&pass=***"),
        ("https://example.org/a.zip?jwt=S&Bearer=S&sas=S&sv=2", "https://example.org/a.zip?jwt=***&Bearer=***&sas=***&sv=2"),
    ],
)
def test_credentials_in_a_url_are_masked_and_the_rest_is_kept(url: str, expected: str) -> None:
    """Userinfo and secret query values go; scheme, host, path, other parameters and the fragment stay."""
    assert redact_url(url) == expected


@pytest.mark.parametrize("url", ["https://example.org/a.zip", "https://example.org/a.zip?version=1.2&arch=x64", "C:\\folder\\a.zip", "not a url", ""])
def test_a_url_without_secrets_is_returned_unchanged(url: str) -> None:
    """Nothing is rewritten when there is nothing to hide (the very same text comes back)."""
    assert redact_url(url) == url


def test_redact_text_finds_urls_in_a_sentence_and_keeps_the_punctuation() -> None:
    """A message that quotes a URL is cleaned; a trailing full stop or bracket is not part of the URL."""
    text = "Cannot fetch https://u:pw@host/x.zip?token=T1. Then (https://host/y?sig=S2) failed; see http://h/z."

    assert redact_text(text) == "Cannot fetch https://***@host/x.zip?token=***. Then (https://host/y?sig=***) failed; see http://h/z."
    assert redact_text("no urls here") == "no urls here"


def serve_secret(world: World, url: str, name: str = "tool", version: str = "1.0.0") -> Path:
    """Publish a pinned version whose download address is *url* (it carries a secret)."""
    data = zip_bytes({"app.exe": "x\n"})
    world.fake.add_url(url, data)
    folder = world.user / name / version
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text(pkg_text(url, sha256=hashlib.sha256(data).hexdigest()), encoding="utf-8")
    return folder


@pytest.mark.parametrize("url", ["https://example.org/files/tool.zip?token=TOPSECRET", "https://example.org/files/tool.zip?X-Amz-Signature=TOPSECRET"])
def test_an_install_of_a_url_with_a_secret_leaves_no_secret_in_receipt_events_or_plan(tmp_path: Path, url: str) -> None:
    """The receipt, every event of the run and the plan data hold the masked address only."""
    world = World(tmp_path)
    serve_secret(world, url)
    events: list[object] = []

    class Collect:
        """Keep every event."""

        def handle(self, event: object) -> None:
            events.append(event)

    plan = lifecycle.plan(world.engine, [Request("install", "tool")])[0].to_data()
    with use_reporter(Collect()):
        result = world.one("install", "tool")

    receipt = (world.user / "tool" / "1.0.0" / "App.info").read_text(encoding="utf-8")
    assert result.ok and "TOPSECRET" not in receipt and "TOPSECRET" not in repr(plan) and "TOPSECRET" not in repr(events)
    assert "***" in receipt and "***" in repr(plan)


def test_an_error_that_quotes_a_secret_url_is_masked(tmp_path: Path) -> None:
    """A download that fails reports the address without its secret, in the message and in the structured error."""
    world = World(tmp_path)
    url = "https://example.org/files/tool.zip?token=TOPSECRET"
    serve_secret(world, url)
    world.fake.urls[url] = OSError(f"connection reset while reading {url}")

    result = world.one("install", "tool")

    assert not result.ok and "TOPSECRET" not in repr(result.error) and "TOPSECRET" not in repr(result.warnings)


def test_the_json_document_of_a_result_redacts_its_warnings() -> None:
    """Defence in depth: a warning that quotes a secret URL is masked in `PackageResult.to_data`, whatever put it there."""
    result = PackageResult("tool", "install", True, "installed", warnings=("fetched https://u:pw@host/a.zip?token=TOPSECRET fine",))

    data = result.to_data()

    assert data["warnings"] == ["fetched https://***@host/a.zip?token=*** fine"] and "TOPSECRET" not in repr(data)
