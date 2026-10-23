"""Cover version discovery by every ``[track]`` provider against recorded vendor responses.

Each case runs a committed format 2 tracker (``tests/fixtures/registry2``) through
``run_tracker`` and ``render_pinned`` with a ``FakeSystem`` that serves the recorded
responses of ``tests/fixtures/http``: the pinned version directory that comes out is what
``gupkg check-updates`` would write. The HTTP client, the providers, the checksum discovery
and the manifest renderer are real. Network, processes and the clock are the only things
replaced. Out of scope: comparing versions with installed ones (the catalog), downloading and
verifying the artifact (issue 027), and the safety of archives (``test_unpack``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, parse_manifest, render_pinned
from gupkg.pkgcheck import check_directory
from gupkg.services.checksum import ChecksumDiscoveryError
from gupkg.services.http import HttpClient
from gupkg.services.track import TrackEnvironment, TrackError, run_tracker
from gupkg.system.fake import FakeHttp, FakeSystem
from tests.http_fixtures import environment, serve, tracker_manifest


def digest(text: str) -> str:
    """Return the SHA-256 the fixture generator derived from *text*."""
    return hashlib.sha256(text.encode()).hexdigest()


def discover(system: FakeSystem, tmp_path: Path, package: str):
    """Run the committed tracker of *package* and render the pinned manifest it would write."""
    manifest = tracker_manifest(package)
    result = run_tracker(manifest, environment(system, tmp_path, package=package))
    return result, render_pinned(manifest, result)


GITHUB = "https://github.com/o/r/releases/download"

# package, recorded cases, discovered version, pinned download_url, discovered sha256 (or None), its origin
CASES = [
    pytest.param("firefox", ["firefox"], "131.0.2",
                 "https://archive.mozilla.org/pub/firefox/releases/131.0.2/win64/en-US/Firefox%20Setup%20131.0.2.exe",
                 digest("firefox-win64-131.0.2"), "vendor-sums", id="firefox-json-and-sums-file"),
    pytest.param("julia", ["julia"], "1.11.0",
                 "https://julialang-s3.julialang.org/bin/winnt/x64/1.11/julia-1.11.0-win64.zip",
                 digest("julia-1.11.0-win64-zip"), "vendor-sums", id="julia-json-records-filter-and-key"),
    pytest.param("vscode", ["vscode"], "1.94.2",
                 "https://update.code.visualstudio.com/1.94.2/win32-x64-archive/stable", None, None, id="vscode-json-first-element"),
    pytest.param("qbittorrent", ["qbittorrent"], "5.0.0",
                 "https://sourceforge.net/projects/qbittorrent/files/qBittorrent/qbittorrent-5.0.0/qbittorrent_5.0.0_x64_setup.exe/download",
                 None, None, id="qbittorrent-json-path-and-match"),
    pytest.param("bulkcrapuninstaller", ["github_bcu"], "5.8.2", f"{GITHUB}/v5.8.2/BCUninstaller_5.8.2_portable.7z",
                 digest("BCUninstaller_5.8.2_portable.7zv5.8.2"), "github-digest", id="github-asset-pattern-with-digest"),
    pytest.param("obsidian", ["github_obsidian"], "1.7.4", f"{GITHUB}/v1.7.4/Obsidian-1.7.4.exe",
                 digest("Obsidian-1.7.4.exev1.7.4"), "github-digest", id="github-exact-asset"),
    pytest.param("keypirinha", ["github_keypirinha"], "2.26", f"{GITHUB}/v2.26/keypirinha-2.26-x64-portable.7z",
                 digest("keypirinha-2.26-x64-portable.7zv2.26"), "github-digest", id="github-asset-with-version"),
    pytest.param("volta", ["github_volta"], "2.0.1", f"{GITHUB}/v2.0.1/volta-2.0.1-windows.zip",
                 digest("volta-2.0.1-windows.zipv2.0.1"), "github-digest", id="github-windows-zip"),
    pytest.param("msys2", ["github_msys2"], "20240925", f"{GITHUB}/2024-09-25/msys2-base-x86_64-20240925.tar.xz",
                 digest("msys2-base-x86_64-20240925.tar.xz2024-09-25"), "github-digest", id="github-newest-matching-dated-tags"),
    pytest.param("restic-backup-gx", ["gitlab_restic"], "1.4.0",
                 "https://gitlab.com/stormking/resticguigx/-/releases/v1.4.0/downloads/ResticGUIx-1.4.0-win64.zip",
                 None, None, id="gitlab-release-link"),
    pytest.param("graphviz", ["page_graphviz"], "12.1.2",
                 "https://gitlab.com/api/v4/projects/4207231/packages/generic/graphviz-releases/12.1.2/windows_10_cmake_Release_Graphviz-12.1.2-win64.zip",
                 None, None, id="page-backreference"),
    pytest.param("hwinfo", ["page_hwinfo"], "10.02", "https://www.hwinfo.com/files/hwi_1002.zip", None, None,
                 id="page-two-digit-major-formats-correctly"),
    pytest.param("lockhunter", ["page_lockhunter"], "3.4.3", "https://lockhunter.com/assets/exe/lockhuntersetup_portable_3-4-3.zip",
                 None, None, id="page-download-template-with-filter"),
    pytest.param("testdisk", ["page_testdisk"], "7.2", "https://www.cgsecurity.org/testdisk-7.2.win64.zip", None, None,
                 id="page-skips-the-wip-prerelease"),
    pytest.param("vlc", ["index_vlc"], "3.0.21",
                 "https://download.videolan.org/pub/videolan/vlc/3.0.21/win64/vlc-3.0.21-win64.zip",
                 digest("vlc-3.0.21-win64.zip"), "vendor-sums", id="index-one-level-and-sidecar"),
    pytest.param("python", ["index_python"], "3.13.0", "https://www.python.org/ftp/python/3.13.0/python-3.13.0-embed-amd64.zip",
                 None, None, id="index-probe-skips-an-incomplete-newest-folder"),
    pytest.param("gimp", ["index_gimp"], "3.0.2", "https://download.gimp.org/gimp/v3.0/gimp-3.0.2-setup.exe", None, None,
                 id="index-two-levels-with-a-key-template"),
    pytest.param("pidgin", ["index_pidgin"], "2.14.13",
                 "https://sourceforge.net/projects/pidgin/files/Pidgin/2.14.13/pidgin-2.14.13-offline.exe/download", None, None,
                 id="index-backtracks-to-the-folder-that-has-the-file"),
    pytest.param("crystaldiskinfo", ["redirect_crystaldiskinfo"], "9.4.2",
                 "https://sourceforge.net/projects/crystaldiskinfo/files/9.4.2/CrystalDiskInfo9_4_2.zip/download", None, None,
                 id="redirect-unwraps-the-url-parameter"),
    pytest.param("zotero", ["redirect_zotero"], "7.0.8", "https://download.zotero.org/client/release/7.0.8/Zotero-7.0.8_win-x64.zip",
                 None, None, id="redirect-final-address"),
    pytest.param("autoruns", ["last_modified_autoruns"], "20240911", "https://download.sysinternals.com/files/Autoruns.zip",
                 None, None, id="last-modified-date-as-version"),
    pytest.param("netio-gui", ["rss_netio"], "1.10", "https://sourceforge.net/projects/netiogui/files/netio-gui_v1.10_portable.zip/download",
                 None, None, id="rss-feed-newest-entry"),
    pytest.param("winscp", ["chain_winscp"], "6.3.5", "https://cdn.winscp.net/files/WinSCP-6.3.5-Portable.zip?secure=abc123&expires=1700000000",
                 None, None, id="chain-page-then-mirror-page"),
    pytest.param("inkscape", ["chain_inkscape"], "1.4", "https://inkscape.org/gallery/item/53697/inkscape-1.4_2024-10-11_86a8ad7-x64.7z",
                 digest("inkscape-1.4-x64"), "vendor-sums", id="chain-three-pages-and-a-digest-file"),
    pytest.param("shutter-encoder", ["chain_shutter"], "18.4", "https://www.shutterencoder.com/sdc_download/1234/?key=abcDEF123",
                 None, None, id="chain-version-from-a-response-header"),
]


@pytest.mark.parametrize(("package", "cases", "version", "download_url", "sha256", "origin"), CASES)
def test_each_registry_tracker_discovers_its_version_and_download_from_recorded_responses(
    system: FakeSystem, tmp_path: Path, package: str, cases: list[str], version: str,
    download_url: str, sha256: str | None, origin: str | None,
) -> None:
    """A tracker finds the newest version, the pinned manifest has the expanded download address and any discovered digest."""
    serve(system, *cases)

    result, pinned = discover(system, tmp_path, package)

    assert result.version == version
    assert pinned.source.download_url == download_url
    assert pinned.source.sha256 == sha256
    assert pinned.source.sha256_from == origin
    assert pinned.kind is ManifestKind.VERSION and pinned.track is None


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
