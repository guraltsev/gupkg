"""Cover `gupkg pkg pin`: copying a receipt's observed hash into a version's `pkg.toml`.

Behaviour covered: the hash lands in `[source]` with `sha256_from = "manual"` and the rest of the
file (comments, other tables) is untouched; a file that already carries the same hash is left alone
and a different one is a conflict; a receipt for another URL, without an observed hash, for a git
or script source, is refused; a shape the line edit cannot handle writes nothing; the file is
written only by `pin_hash`; and nothing else (observing a hash during a skipped download) ever writes
into `pkg.toml`.

Real: the pinning module, the manifest parser as the proof of the edit, and real files in a temporary
folder. Mocked: the downloads (a `FakeSystem` URL fixture). Out of scope: the `gupkg pkg pin` command line (issue 029).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gupkg.model.checksum import SKIPPED_CLI, VERIFIED, ChecksumRecord
from gupkg.model.manifest import parse_manifest
from gupkg.pinning import PinError, pin_hash, pin_text
from gupkg.services.artifact import fetch_artifact
from gupkg.services.http import HttpClient
from gupkg.system.fake import FakeHttp, FakeSystem

URL = "https://example.org/tool-1.0.zip"
BODY = b"payload bytes " * 100
OBSERVED = hashlib.sha256(BODY).hexdigest()

UNPINNED = f"""\
format = 2
description = "A tool"  # keep this comment

[source]
download_url = "{URL}"
strip = 1

[[shim]]
name = "tool"
target = "$App\\\\tool.exe"
"""


def skipped(url: str = URL, observed: str = OBSERVED) -> ChecksumRecord:
    """Return the record of a download that was installed with `--skip-checksum`."""
    return ChecksumRecord(SKIPPED_CLI, url=url, final_url=url, observed_sha256=observed, size=len(BODY))


def test_pinning_sets_the_hash_and_manual_provenance_and_keeps_everything_else() -> None:
    """The result differs from the original by exactly the two keys, and comments survive."""
    result = pin_text(UNPINNED, skipped())

    assert result.changed is True
    after = parse_manifest(result.text)
    assert (after.source.sha256, after.source.sha256_from) == (OBSERVED, "manual")
    assert "# keep this comment" in result.text
    assert result.text.replace(f'sha256 = "{OBSERVED}"\nsha256_from = "manual"\n', "") == UNPINNED


def test_pinning_keeps_windows_line_endings() -> None:
    """A CRLF file stays CRLF; the inserted lines use the same ending."""
    result = pin_text(UNPINNED.replace("\n", "\r\n"), skipped())

    assert "\r\n" in result.text and "\n" not in result.text.replace("\r\n", "")


def test_the_same_hash_is_a_no_op_and_a_different_one_is_a_conflict() -> None:
    """Pinning twice changes nothing the second time; a file with another hash is never overwritten."""
    pinned = pin_text(UNPINNED, skipped()).text

    assert pin_text(pinned, skipped()).changed is False
    with pytest.raises(PinError, match="already declares"):
        pin_text(pinned, skipped(observed="cd" * 32))


def test_a_declared_hash_with_another_case_counts_as_the_same_hash() -> None:
    """The manifest normalises its digest, so an upper-case declaration equals the observed one."""
    text = UNPINNED.replace('strip = 1', f'sha256 = "{OBSERVED.upper()}"\nstrip = 1')

    assert pin_text(text, skipped()).changed is False


@pytest.mark.parametrize(
    ("record", "problem"),
    [
        pytest.param(skipped(url="https://other.example/tool-1.0.zip"), "another address", id="receipt-for-another-url"),
        pytest.param(ChecksumRecord("origin-not-verified"), "nothing to pin", id="no-download-decision"),
        pytest.param(ChecksumRecord(VERIFIED, commit="0" * 40), "nothing to pin", id="git-record-without-a-hash"),
    ],
)
def test_a_receipt_that_cannot_justify_a_pin_is_refused(record: ChecksumRecord, problem: str) -> None:
    """Only an observed hash of the same download is pinned."""
    with pytest.raises(PinError, match=problem):
        pin_text(UNPINNED, record)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        pytest.param('format = 2\n[source]\nscript = "pkg.local/populate.py"\n', "no download_url", id="script-source"),
        pytest.param('format = 2\n', "no download_url", id="no-source"),
        pytest.param(
            f'format = 2\n[source]\nkind = "git"\ndownload_url = "{URL}"\n[source.git]\ncommit = "{"1" * 40}"\n',
            "pinned by its commit", id="git-source",
        ),
        pytest.param("format = 2\nthis is not toml", "not a valid version manifest", id="invalid-manifest"),
    ],
)
def test_a_manifest_that_has_nothing_to_pin_is_refused(text: str, problem: str) -> None:
    """Scripts, git sources and invalid files are never edited."""
    with pytest.raises(PinError, match=problem):
        pin_text(text, skipped())


def test_a_source_written_as_an_inline_table_is_refused_without_writing(tmp_path: Path) -> None:
    """A shape the line edit cannot handle fails and leaves the file byte-for-byte unchanged."""
    text = f'format = 2\nsource = {{ download_url = "{URL}" }}\n'
    (tmp_path / "pkg.toml").write_text(text, encoding="utf-8")

    with pytest.raises(PinError):
        pin_hash(tmp_path, skipped())

    assert (tmp_path / "pkg.toml").read_text(encoding="utf-8") == text


def test_pin_hash_writes_the_file_and_a_second_call_changes_nothing(tmp_path: Path) -> None:
    """The file on disk is replaced once; the second pin reports no change."""
    path = tmp_path / "pkg.toml"
    path.write_text(UNPINNED, encoding="utf-8")

    first = pin_hash(tmp_path, skipped())
    written = path.read_text(encoding="utf-8")
    second = pin_hash(tmp_path, skipped())

    assert first.changed and not second.changed
    assert parse_manifest(written).source.sha256 == OBSERVED and path.read_text(encoding="utf-8") == written


def test_pin_hash_reports_a_missing_file(tmp_path: Path) -> None:
    """A version directory without pkg.toml is a PinError."""
    with pytest.raises(PinError, match="cannot read"):
        pin_hash(tmp_path, skipped())


def test_a_skipped_download_writes_nothing_into_the_version_directory(system: FakeSystem, tmp_path: Path) -> None:
    """Observing a hash during a `--skip-checksum` download leaves `pkg.toml` byte-for-byte as it was."""
    version = tmp_path / "tool" / "1.0"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(UNPINNED, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in version.iterdir()}
    system.urls[URL] = FakeHttp(BODY)
    client = HttpClient(system, env={}, sleep=lambda seconds: None, cache_dir=tmp_path / "cache", retries=0)

    fetched = fetch_artifact(client, URL, expected_sha256=None, skip=SKIPPED_CLI)

    assert fetched.record.observed_sha256 == OBSERVED
    assert {path.name: path.read_bytes() for path in version.iterdir()} == before
    assert "sha256" not in (version / "pkg.toml").read_text(encoding="utf-8")


def test_pinning_the_observed_hash_makes_the_next_download_verified(system: FakeSystem, tmp_path: Path) -> None:
    """End to end: skip once, pin the receipt's hash, and the same URL then verifies with no flag."""
    system.urls[URL] = FakeHttp(BODY)
    client = HttpClient(system, env={}, sleep=lambda seconds: None, cache_dir=tmp_path / "cache", retries=0)
    first = fetch_artifact(client, URL, expected_sha256=None, skip=SKIPPED_CLI)
    pinned = parse_manifest(pin_text(UNPINNED, first.record).text).source

    second = fetch_artifact(client, URL, expected_sha256=pinned.sha256, sha256_from=pinned.sha256_from)

    assert second.record.decision == VERIFIED and second.record.sha256_from == "manual"
