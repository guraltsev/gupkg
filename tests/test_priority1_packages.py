"""Cover the Priority 1 package templates from issue 014.

Behavior covered: every new template normalizes as a self-updating package
definition with a description, and each package-local update checker turns a
publisher's page, release record, or redirect into the right update candidate
(or reports nothing to do when the installed version is current).

Publisher HTTP responses are mocked; the checked-in manifests and checker
modules are real. Downloading, checksum enforcement, extraction, and whether a
publisher's site is still reachable are out of scope.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tomllib
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

import pytest

from gupkg.configuration import normalize_runtime_config
from gupkg.core import PackageIdentity


ROOT = Path(__file__).resolve().parents[1]
PKGS = ROOT / "pkgs"

NEW_PACKAGES = [
    "autoruns", "bulkcrapuninstaller", "crystaldiskinfo", "crystaldiskmark",
    "czkawka", "emacs", "geogebra", "geogebra5", "graphviz", "gsmartcontrol", "hwinfo",
    "hwmonitor", "imagemagick", "inkscape", "irfanview", "keypirinha", "lockhunter", "nano", "notepadplusplus",
    "obsidian", "obsstudio", "openhardwaremonitor", "paintdotnet",
    "processexplorer", "quarto", "rapidee", "rufus", "sharex", "testdisk",
    "windowsterminal", "winmerge", "winscp", "wumgr", "xournalpp", "zotero",
]


def _load_checker(name: str):
    """Load one package-local checker the way the update coordinator does."""
    path = PKGS / name / "pkg.bootstrap" / "pkg.local" / "check_update.py"
    spec = importlib.util.spec_from_file_location(f"{name}_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(body: str | bytes) -> io.BytesIO:
    """Return a context-manager response whose body is ``body``."""
    data = body.encode() if isinstance(body, str) else body
    return mock.MagicMock(**{"read.return_value": data, "__enter__.return_value.read.return_value": data})


@pytest.mark.parametrize("name", NEW_PACKAGES)
def test_template_is_a_described_self_updating_package(name: str) -> None:
    """Each new template has a description and discovers its own updates."""
    package = PKGS / name / "pkg.bootstrap"
    identity = PackageIdentity.from_version_path(package.parent, package, is_current=False)
    raw = tomllib.loads((package / "pkg.toml").read_text(encoding="utf-8"))
    config = normalize_runtime_config(raw, identity)

    assert raw["description"].strip()
    assert config["update"]["check"]["mode"] in {"github", "module"}
    assert config["update"]["payload"]["mode"] in {"zip", "module"}


def test_page_checker_picks_the_newest_release_link() -> None:
    """TestDisk resolves the highest version listed on the download page."""
    checker = _load_checker("testdisk")
    page = (
        '<a href="x/testdisk-7.2.win64.zip">7.2</a>'
        '<a href="x/testdisk-7.3-WIP.win64.zip">7.3</a>'
    )
    with mock.patch.object(checker.urllib.request, "urlopen", return_value=_response(page)):
        candidate = checker.check_update({"current": {"version": "7.2"}})

    assert candidate["version"] == "7.3-WIP"
    assert candidate["url"] == "https://www.cgsecurity.org/testdisk-7.3-WIP.win64.zip"


def test_page_checker_reports_nothing_when_current_and_healthy() -> None:
    """An installed, healthy newest release produces no candidate."""
    checker = _load_checker("testdisk")
    page = '<a href="testdisk-7.3-WIP.win64.zip">7.3</a>'
    with mock.patch.object(checker.urllib.request, "urlopen", return_value=_response(page)):
        assert checker.check_update({"current": {"version": "7.3-WIP", "appReady": True}}) is None
        assert checker.check_update({"current": {"version": "7.3-WIP", "appReady": False}}) is not None


def test_hwinfo_uses_the_us_mirror_link_and_decimal_version() -> None:
    """HWiNFO's 'Local (U.S.)' link supplies the file; 854 means 8.54."""
    checker = _load_checker("hwinfo")
    page = (
        '<a href="https://www.hwinfo.com/files/hwi_855_6095.zip">Beta</a>'
        '<a href="https://www.hwinfo.com/files/hwi_854.zip" target="_blank">Local (U.S.)</a>'
    )
    with mock.patch.object(checker.urllib.request, "urlopen", return_value=_response(page)):
        candidate = checker.check_update({"current": {"version": "bootstrap"}})

    assert candidate["version"] == "8.54"
    assert candidate["url"] == "https://www.hwinfo.com/files/hwi_854.zip"


def test_winscp_requests_the_mirror_link_for_the_portable_zip() -> None:
    """WinSCP's candidate URL is the mirror link issued for the portable ZIP."""
    checker = _load_checker("winscp")
    listing = (
        '<a href="/download/WinSCP-6.7-Portable-x64-Experimental.zip/download">x</a>'
        '<a href="/download/WinSCP-6.7-Portable.zip/download">WinSCP-6.7-Portable.zip</a>'
    )
    mirror = '<a href="https://cdn.winscp.net/files/WinSCP-6.7-Portable.zip?secure=abc&amp;t=1">go</a>'
    with mock.patch.object(
        checker.urllib.request, "urlopen", side_effect=[_response(listing), _response(mirror)]
    ):
        candidate = checker.check_update({"current": {"version": "6.3.5"}})

    assert candidate["version"] == "6.7"
    assert candidate["url"] == "https://cdn.winscp.net/files/WinSCP-6.7-Portable.zip?secure=abc&t=1"


def _release(tag: str, assets: list[dict]) -> mock.MagicMock:
    """Return a mocked GitHub latest-release response."""
    return _response(json.dumps({"tag_name": tag, "id": 7, "assets": assets}))


def test_github_checker_matches_an_asset_whose_name_differs_from_the_tag() -> None:
    """Bulk Crap Uninstaller's tag v6.3 selects the 6.3.0 portable archive."""
    checker = _load_checker("bulkcrapuninstaller")
    digest = "ab" * 32
    assets = [
        {"name": "BCUninstaller_6.3.0_setup.exe", "state": "uploaded", "browser_download_url": "https://x/setup"},
        {
            "name": "BCUninstaller_6.3.0_portable.7z",
            "state": "uploaded",
            "browser_download_url": "https://x/portable.7z",
            "digest": f"sha256:{digest}",
        },
    ]
    with mock.patch.object(checker.urllib.request, "urlopen", return_value=_release("v6.3", assets)):
        candidate = checker.check_update({"current": {"version": "bootstrap"}})

    assert candidate["version"] == "6.3"
    assert candidate["url"] == "https://x/portable.7z"
    assert candidate["sha256"] == digest


def test_github_checker_rejects_a_release_without_the_expected_asset() -> None:
    """A release lacking the portable archive fails instead of picking another file."""
    checker = _load_checker("wumgr")
    assets = [{"name": "Translation.ini", "state": "uploaded", "browser_download_url": "https://x/t"}]
    with mock.patch.object(checker.urllib.request, "urlopen", return_value=_release("v1.1", assets)):
        with pytest.raises(RuntimeError, match="exactly one asset"):
            checker.check_update({"current": {"version": "bootstrap"}})


def test_modification_date_becomes_the_version() -> None:
    """Autoruns, which has no version in its URL, is versioned by file date."""
    checker = _load_checker("autoruns")
    headers = {"Last-Modified": "Wed, 17 Jun 2026 12:00:00 GMT"}
    response = mock.MagicMock()
    response.__enter__.return_value.headers = headers
    with mock.patch.object(checker.urllib.request, "urlopen", return_value=response):
        candidate = checker.check_update({"current": {"version": "20260101"}})
        unchanged = checker.check_update({"current": {"version": "20260617", "appReady": True}})

    assert candidate["version"] == "20260617"
    assert unchanged is None


def _redirect(location: str | None, code: int = 302) -> urllib.error.HTTPError:
    """Return the HTTP error urllib raises for an unfollowed redirect."""
    headers = Message()
    if location:
        headers["Location"] = location
    return urllib.error.HTTPError("https://start", code, "redirect", headers, None)


def _redirecting_opener(*outcomes: Exception) -> mock.MagicMock:
    """Return a fake URL opener whose requests raise the given outcomes in order."""
    opener = mock.MagicMock()
    opener.open.side_effect = list(outcomes)
    return opener


def test_redirect_checker_unwraps_the_real_target_of_an_ad_redirect() -> None:
    """CrystalDiskInfo's version comes from the SourceForge URL inside the wrapper."""
    checker = _load_checker("crystaldiskinfo")
    wrapped = (
        "https://crystalmark.info/ad_redirect.php?url="
        "https%3A%2F%2Fsourceforge.net%2Fprojects%2Fcrystaldiskinfo%2Ffiles%2F9.9.3%2F"
        "CrystalDiskInfo9_9_3.zip%2Fdownload"
    )
    with mock.patch.object(
        checker.urllib.request, "build_opener", return_value=_redirecting_opener(_redirect(wrapped))
    ):
        candidate = checker.check_update({"current": {"version": "9.9.0"}})

    assert candidate["version"] == "9.9.3"
    assert candidate["fileName"] == "CrystalDiskInfo9_9_3.zip"
    assert candidate["url"].startswith("https://sourceforge.net/projects/crystaldiskinfo/files/9.9.3/")


def test_redirect_checker_retries_a_transient_server_error() -> None:
    """GeoGebra's intermittent 502 does not fail the update check."""
    checker = _load_checker("geogebra")
    target = "/installers/6.0/GeoGebra-Windows-Portable-6-0-932-1.zip"
    opener = _redirecting_opener(_redirect(None, 502), _redirect(target), mock.MagicMock())
    with mock.patch.object(checker.urllib.request, "build_opener", return_value=opener):
        candidate = checker.check_update({"current": {"version": "bootstrap"}})

    assert candidate["version"] == "6.0.932.1"
    assert candidate["url"] == "https://download.geogebra.org" + target


def test_inkscape_follows_the_release_pages_to_the_file_and_digest() -> None:
    """Inkscape's newest release resolves to its 64-bit 7-Zip file and posted digest."""
    checker = _load_checker("inkscape")
    releases = (
        '<a href="/release/inkscape-1.4.3/">1.4.3</a>'
        '<a href="/en/release/inkscape-1.4.4/">1.4.4</a>'
        '<a href="/release/inkscape-1.4/">1.4</a>'
    )
    digest = "cd" * 32
    download = (
        '<a href="/gallery/item/59503/inkscape-1.4.4_2026-05-05_abc-x64_xyz.7z">file</a>'
        '<a href="https://media.inkscape.org/media/resources/sigs/inkscape-1.4.4-x64.7z_q.sha256">sum</a>'
    )
    responses = [_response(releases), _response(download), _response(f"{digest}  inkscape.7z\n")]
    with mock.patch.object(checker.urllib.request, "urlopen", side_effect=responses):
        candidate = checker.check_update({"current": {"version": "1.4.3"}})

    assert candidate["version"] == "1.4.4"
    assert candidate["url"] == "https://inkscape.org/gallery/item/59503/inkscape-1.4.4_2026-05-05_abc-x64_xyz.7z"
    assert candidate["sha256"] == digest


def test_emacs_picks_the_newest_ucrt64_zip_and_its_published_digest() -> None:
    """Emacs resolves to the newest folder's highest UCRT64 ZIP, skipping nodeps files."""
    checker = _load_checker("emacs")
    root = '<a href="emacs-30/">emacs-30/</a><a href="emacs-31/">emacs-31/</a>'
    listing = (
        '<a href="emacs-31.1-UCRT64.zip">x</a><a href="emacs-31.1_1-UCRT64.zip">x</a>'
        '<a href="emacs-31.1_1-nodeps-UCRT64.zip">x</a><a href="emacs-31.1_1.zip">x</a>'
    )
    digest = "ef" * 32
    sums = f"{digest} *emacs-31.1_1-UCRT64.zip\n{'00' * 32} *emacs-31.1_1.zip\n"
    with mock.patch.object(
        checker.urllib.request, "urlopen",
        side_effect=[_response(root), _response(listing), _response(sums)],
    ):
        candidate = checker.check_update({"current": {"version": "31.0.91"}})

    assert candidate["version"] == "31.1_1"
    assert candidate["url"] == "https://ftp.gnu.org/gnu/emacs/windows/emacs-31/emacs-31.1_1-UCRT64.zip"
    assert candidate["sha256"] == digest


def test_emacs_candidate_has_no_digest_when_gnu_publishes_none() -> None:
    """A build without a checksum file is still offered, just unverified."""
    checker = _load_checker("emacs")
    missing = urllib.error.URLError("not found")
    with mock.patch.object(
        checker.urllib.request, "urlopen",
        side_effect=[
            _response('<a href="emacs-31/">x</a>'),
            _response('<a href="emacs-31.1-UCRT64.zip">x</a>'),
            missing,
            missing,
        ],
    ):
        candidate = checker.check_update({"current": {"version": "bootstrap"}})

    assert candidate["version"] == "31.1"
    assert "sha256" not in candidate
