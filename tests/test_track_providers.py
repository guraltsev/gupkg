"""Cover version discovery by every ``[track]`` provider against fixture vendor responses.

Each case runs a committed format 2 tracker (``tests/fixtures/registry2``) through
``run_tracker`` and ``render_pinned`` with a ``FakeSystem`` that serves the responses of
``tests/fixtures/http``: the pinned version directory that comes out is what
``gupkg check-updates`` would write. The fixtures are synthetic models of the registry hooks'
regular expressions, marked ``recorded: null`` (a test asserts the marker); they are re-recorded
by ``tools/record_http_fixtures.py``, and each carries its own `expected` block. The HTTP client, the providers, the checksum discovery
and the manifest renderer are real. Network, processes and the clock are the only things
replaced. Out of scope: comparing versions with installed ones (the catalog), downloading and
verifying the artifact (issue 027), and the safety of archives (``test_unpack``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, parse_manifest, render_pinned
from gupkg.pkgcheck import check_directory
from gupkg.services.checksum import ChecksumDiscoveryError
from gupkg.services.http import HttpClient
from gupkg.services.track import TrackEnvironment, TrackError, run_tracker
from gupkg.system.fake import FakeHttp, FakeSystem
from tests.http_fixtures import HTTP_CASES, REGISTRY2, environment, serve, tracker_manifest


def discover(system: FakeSystem, tmp_path: Path, package: str):
    """Run the committed tracker of *package* and render the pinned manifest it would write."""
    manifest = tracker_manifest(package)
    result = run_tracker(manifest, environment(system, tmp_path, package=package))
    return result, render_pinned(manifest, result)


CASES = sorted(HTTP_CASES.glob("*.json"))


@pytest.mark.parametrize("case", [pytest.param(path, id=path.stem) for path in CASES])
def test_each_tracker_discovers_its_version_and_download_from_the_fixture_responses(
    system: FakeSystem, tmp_path: Path, case: Path
) -> None:
    """A tracker finds the newest version; the pinned manifest has the expanded download address and any discovered digest.

    The expected values live in the fixture's own `expected` block, so re-recording a case needs no code edit.
    """
    document = json.loads(case.read_text(encoding="utf-8"))
    expected = document["expected"]
    serve(system, case.stem)

    result, pinned = discover(system, tmp_path, document["tracker"])

    assert result.version == expected["version"]
    assert pinned.source.download_url == expected["download_url"]
    assert pinned.source.sha256 == expected["sha256"]
    assert pinned.source.sha256_from == expected["sha256_from"]
    assert pinned.kind is ManifestKind.VERSION and pinned.track is None


def test_synthetic_fixtures_say_so_and_no_fixture_claims_a_recording_it_does_not_have() -> None:
    """A fixture is either a synthetic model marked `recorded: null`, or carries a recording date, never both or neither."""
    assert len(CASES) == 25
    for case in CASES:
        document = json.loads(case.read_text(encoding="utf-8"))
        if document["recorded"] is None:
            assert document["origin"].startswith("synthetic fixture modelled on the registry hook"), case.name
            assert "to be re-recorded by tools/record_http_fixtures.py" in document["origin"], case.name
        else:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", document["recorded"]), case.name
            assert document["origin"].startswith("recorded from the live sites"), case.name
        assert (REGISTRY2 / document["tracker"] / "_check_updates" / "pkg.toml").is_file(), case.name


def test_the_page_provider_sends_the_package_headers_on_its_request(system: FakeSystem, tmp_path: Path) -> None:
    """A tracker's `[track.headers]` replace the default User-Agent on the page request, and the pinned source keeps the download headers."""
    serve(system, "page_hwinfo")

    _result, pinned = discover(system, tmp_path, "hwinfo")

    sent = system.request_log[0].headers
    assert sent["User-Agent"] == "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gupkg-update-check/1"
    assert pinned.source.headers["Referer"] == "https://www.hwinfo.com/download/"


def test_providers_choose_among_matches_by_version_order_not_text_order(system: FakeSystem, tmp_path: Path) -> None:
    """`1.10` is newer than `1.3` and `3.0.21` newer than `3.0.9.2`: selection uses the shared version comparison."""
    serve(system, "rss_netio", "index_vlc")

    rss, _ = discover(system, tmp_path, "netio-gui")
    vlc, _ = discover(system, tmp_path, "vlc")

    assert (rss.version, vlc.version) == ("1.10", "3.0.21")


def test_the_github_provider_skips_prereleases_and_releases_without_the_asset(system: FakeSystem, tmp_path: Path) -> None:
    """With `newest-matching`, the nightly prerelease and the newest tag that lacks the tarball are passed over."""
    serve(system, "github_msys2")

    result, _ = discover(system, tmp_path, "msys2")

    assert result.captures["tag"] == "2024-09-25" and result.captures["asset_name"] == "msys2-base-x86_64-20240925.tar.xz"


def test_a_newer_prerelease_is_ignored_unless_the_tracker_asks_for_prereleases(system: FakeSystem, tmp_path: Path) -> None:
    """`prerelease = false` (the default) passes over a newer release candidate; `prerelease = true` takes it."""
    releases = json.dumps([
        {"tag_name": "v2.0.0-rc1", "prerelease": True, "draft": False, "assets": []},
        {"tag_name": "v1.9.0", "prerelease": False, "draft": False, "assets": []},
        {"tag_name": "v2.1.0", "prerelease": False, "draft": True, "assets": []},
    ])
    system.urls["https://api.github.com/repos/o/r/releases?per_page=100"] = releases

    def versions(extra: str) -> str:
        """Run a tracker for o/r with the given extra track keys and return its version."""
        text = "\n".join([
            "format = 2", "[track]", 'provider = "github"', 'repo = "o/r"', 'release = "newest-matching"', extra,
            "[source]", 'download_url = "https://example.org/x"',
        ])
        manifest = parse_manifest(text, ManifestKind.TRACKER)
        return run_tracker(manifest, environment(system, tmp_path)).version

    assert versions("") == "1.9.0"
    assert versions("prerelease = true") == "2.0.0-rc1"


def test_the_github_provider_sends_the_token_only_to_github_over_https(system: FakeSystem, tmp_path: Path) -> None:
    """GITHUB_TOKEN reaches api.github.com; a client without it sends no Authorization header at all."""
    serve(system, "github_volta")
    manifest = tracker_manifest("volta")
    with_token = TrackEnvironment(system, HttpClient(system, env={"GITHUB_TOKEN": "t0ken"}, sleep=lambda seconds: None))
    env = with_token

    run_tracker(manifest, env)
    run_tracker(manifest, environment(system, tmp_path))

    assert system.request_log[0].headers["Authorization"] == "Bearer t0ken"
    assert "Authorization" not in system.request_log[1].headers


@pytest.mark.parametrize(
    ("package", "case", "damage", "message"),
    [
        pytest.param("firefox", "firefox", ("https://product-details.mozilla.org/1.0/firefox_versions.json", "{}"),
                     "No record", id="json-without-the-version-key"),
        pytest.param("graphviz", "page_graphviz", ("https://graphviz.org/download/", "<html>nothing</html>"),
                     "matched nothing", id="page-without-a-match"),
        pytest.param("obsidian", "github_obsidian",
                     ("https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest", '{"tag_name": "v1", "assets": []}'),
                     "no release with a usable version", id="github-release-without-the-asset"),
        pytest.param("netio-gui", "rss_netio", ("https://sourceforge.net/projects/netiogui/rss?path=/", "<rss><channel>"),
                     "not well-formed", id="rss-that-is-not-xml"),
        pytest.param("netio-gui", "rss_netio",
                     ("https://sourceforge.net/projects/netiogui/rss?path=/", '<!DOCTYPE rss [<!ENTITY x "y">]><rss/>'),
                     "document type declaration", id="rss-with-a-doctype"),
    ],
)
def test_a_provider_reports_a_clear_error_for_a_response_it_cannot_use(
    system: FakeSystem, tmp_path: Path, package: str, case: str, damage: tuple[str, str], message: str
) -> None:
    """A response that lacks what the pattern, path or asset needs fails with a message, never with a guess."""
    serve(system, case)
    system.urls[damage[0]] = FakeHttp(damage[1])

    with pytest.raises(TrackError, match=message):
        discover(system, tmp_path, package)


def test_a_configured_checksum_source_without_a_digest_is_an_error_not_a_silent_skip(system: FakeSystem, tmp_path: Path) -> None:
    """When `[source.checksum]` names a sums file that lacks the entry, discovery fails and says where it looked."""
    serve(system, "firefox")
    system.urls["https://archive.mozilla.org/pub/firefox/releases/131.0.2/SHA256SUMS"] = FakeHttp("0" * 64 + "  other.exe\n")

    with pytest.raises(ChecksumDiscoveryError, match="SHA256SUMS"):
        discover(system, tmp_path, "firefox")


def test_a_version_the_page_supplies_must_be_a_safe_directory_name(system: FakeSystem, tmp_path: Path) -> None:
    """A page that "finds" a version with path characters is refused instead of becoming a directory name."""
    serve(system, "firefox")
    system.urls["https://product-details.mozilla.org/1.0/firefox_versions.json"] = FakeHttp('{"LATEST_FIREFOX_VERSION": "..\\\\evil"}')

    with pytest.raises(TrackError, match="version"):
        discover(system, tmp_path, "firefox")


def test_a_tracker_template_that_names_an_unproduced_value_is_an_error_at_validation(tmp_path: Path) -> None:
    """A `[source]` that refers to `${nothing}` the provider never produces is reported by the validator, before any request."""
    folder = tmp_path / "pkg" / "_check_updates"
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text(
        'format = 2\n[track]\nprovider = "github"\nrepo = "o/r"\nasset = "x-${version}.zip"\n'
        '[source]\ndownload_url = "${nothing}"\n', encoding="utf-8")

    report = check_directory(folder)

    assert any("${nothing}" in error and "github" in error for error in report.errors)
