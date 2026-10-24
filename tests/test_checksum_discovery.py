"""Cover checksum discovery: sidecar, sums file, JSON and page sources, and the sums-file reader.

Discovery runs through the real client over ``FakeSystem`` URL fixtures. It only *finds* a
digest and names its provenance; verifying a download and the missing-checksum policy belong
to issue 027 and are out of scope here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.manifest import Checksum
from gupkg.services.checksum import ChecksumDiscoveryError, discover_checksum, find_digest
from gupkg.services.http import HttpClient
from gupkg.system.fake import FakeHttp, FakeSystem

HEX_A, HEX_B = "a1" * 32, "b2" * 32
VALUES = {"version": "1.2", "download_url": "https://example.org/dl/tool-1.2.zip", "page_url": "https://example.org/page"}


def discover(system: FakeSystem, tmp_path: Path, checksum: Checksum, values: dict[str, str] = VALUES):
    """Run discovery with a client that never sleeps."""
    return discover_checksum(HttpClient(system, env={}, sleep=lambda seconds: None, retries=0), checksum, values)


@pytest.mark.parametrize(
    ("body", "digest"),
    [
        pytest.param(f"{HEX_A}\n", HEX_A, id="digest-only"),
        pytest.param(f"{HEX_A}  tool-1.2.zip\n", HEX_A, id="gnu-format"),
        pytest.param(f"{HEX_B}  other.zip\n{HEX_A.upper()} *tool-1.2.zip\n", HEX_A, id="several-lines-picks-the-download"),
        pytest.param(f"SHA256 (tool-1.2.zip) = {HEX_A}\n", HEX_A, id="bsd-format"),
    ],
)
def test_a_sidecar_next_to_the_download_supplies_the_digest(system: FakeSystem, tmp_path: Path, body: str, digest: str) -> None:
    """`sidecar = "${download_url}.sha256"` reads the digest in any of the usual layouts, lower-cased."""
    system.urls["https://example.org/dl/tool-1.2.zip.sha256"] = body

    found = discover(system, tmp_path, Checksum(sidecar="${download_url}.sha256"))

    assert (found.sha256, found.sha256_from) == (digest, "vendor-sums")


def test_a_sidecar_with_two_unrelated_digests_is_ambiguous(system: FakeSystem, tmp_path: Path) -> None:
    """Two different digests and no line for the file give no answer instead of a guess."""
    system.urls["https://example.org/dl/tool-1.2.zip.sha256"] = f"{HEX_A}  x.zip\n{HEX_B}  y.zip\n"

    with pytest.raises(ChecksumDiscoveryError, match="does not contain a SHA-256"):
        discover(system, tmp_path, Checksum(sidecar="${download_url}.sha256"))


def test_a_sums_file_entry_is_found_by_its_exact_name(system: FakeSystem, tmp_path: Path) -> None:
    """`sums_file` with `entry` matches the whole listed path, so `win32/…` is never taken for `win64/…`."""
    system.urls["https://example.org/SHA256SUMS"] = f"{HEX_B}  win32/Setup 1.2.exe\n{HEX_A}  win64/Setup 1.2.exe\n"

    found = discover(system, tmp_path, Checksum(sums_file="https://example.org/SHA256SUMS", entry="win64/Setup ${version}.exe"))

    assert found.sha256 == HEX_A


def test_a_sums_file_without_the_entry_is_an_error_naming_where_it_looked(system: FakeSystem, tmp_path: Path) -> None:
    """A missing entry is a ChecksumDiscoveryError (027 decides what a missing hash means), not a silent `None`."""
    system.urls["https://example.org/SHA256SUMS"] = f"{HEX_B}  other.exe\n"

    with pytest.raises(ChecksumDiscoveryError, match=r"SHA256SUMS.*Setup 1\.2\.exe"):
        discover(system, tmp_path, Checksum(sums_file="https://example.org/SHA256SUMS", entry="Setup ${version}.exe"))


def test_a_json_document_supplies_the_digest_by_path(system: FakeSystem, tmp_path: Path) -> None:
    """`json` and `path` read the digest out of a JSON document; a `sha256:` prefix is accepted."""
    system.urls["https://example.org/meta.json"] = f'{{"files": [{{"name": "a"}}, {{"name": "tool", "hash": "sha256:{HEX_A}"}}]}}'

    found = discover(system, tmp_path, Checksum(json="https://example.org/meta.json", path="$.files[?name==tool].hash"))

    assert found.sha256 == HEX_A


def test_a_page_pattern_supplies_the_digest_from_the_page_the_version_came_from(system: FakeSystem, tmp_path: Path) -> None:
    """`page_match` runs on the tracker's page with the version substituted (escaped) into the pattern."""
    system.urls["https://example.org/page"] = f"<td>tool-1.2.zip</td><td>{HEX_A}</td> <td>tool-1x2.zip</td><td>{HEX_B}</td>"

    found = discover(system, tmp_path, Checksum(page_match="tool-${version}\\.zip</td><td>(?P<sha256>[0-9a-f]{64})"))

    assert found.sha256 == HEX_A


def test_page_match_without_a_page_is_an_error(system: FakeSystem, tmp_path: Path) -> None:
    """A provider that read no page (for example `json`) cannot be combined with `page_match`."""
    with pytest.raises(ChecksumDiscoveryError, match="has none"):
        discover(system, tmp_path, Checksum(page_match="(?P<sha256>x)"), {"version": "1"})


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not a digest", id="no-digest"),
        pytest.param("g" * 64, id="not-hex"),
        pytest.param("a" * 63, id="too-short"),
        pytest.param("a" * 65, id="too-long"),
    ],
)
def test_only_a_64_digit_hexadecimal_token_is_accepted(system: FakeSystem, tmp_path: Path, body: str) -> None:
    """Text that is not exactly a SHA-256 can never become a discovered digest."""
    system.urls["https://example.org/dl/tool-1.2.zip.sha256"] = body

    with pytest.raises(ChecksumDiscoveryError):
        discover(system, tmp_path, Checksum(sidecar="${download_url}.sha256"))


def test_an_unreachable_checksum_source_is_a_discovery_error(system: FakeSystem, tmp_path: Path) -> None:
    """A 404 or a dropped connection while fetching the sums file is reported as ChecksumDiscoveryError."""
    system.urls["https://example.org/dl/tool-1.2.zip.sha256"] = FakeHttp("", 404)

    with pytest.raises(ChecksumDiscoveryError, match="HTTP 404"):
        discover(system, tmp_path, Checksum(sidecar="${download_url}.sha256"))


def test_find_digest_without_a_name_needs_exactly_one_digest() -> None:
    """With no file name, a text that holds one digest returns it and a text with two returns nothing."""
    assert find_digest(f"  {HEX_A}  \n", None) == HEX_A
    assert find_digest(f"{HEX_A} {HEX_B}", None) is None


def test_find_digest_is_linear_on_a_huge_hostile_file() -> None:
    """A multi-megabyte file of near-digests is read without hanging (every pattern is anchored)."""
    text = ("a" * 63 + "\n") * 200_000

    assert find_digest(text, "x.zip") is None
