"""Cover the service hints that ``gupkg pkg check`` gives for hook scripts.

The hooks scanned are real registry hooks committed under ``tests/fixtures/hints``
(18 files, one folder per package). Covers which service each known hook is recognised as, that a hook of no known
pattern (GIMP's vendor-installer unpack) gets no hint, that ``pkg check`` reports a hint as a
warning with the replacement, and that the hint count supports the epic's goal that most
hooks are replaceable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.pkgcheck import check_directory
from gupkg.services.hints import scan_directory, scan_text

REGISTRY = Path(__file__).resolve().parent / "fixtures" / "hints"
HOOKS = sorted(REGISTRY.glob("*/*.py"))


def services_of(package: str, hook: str) -> list[str]:
    """Return the services hinted for one committed registry hook."""
    path = REGISTRY / package / hook
    return [hint.service for hint in scan_text(path.read_text(encoding="utf-8-sig"), hook)]


@pytest.mark.parametrize(
    ("package", "hook", "expected"),
    [
        pytest.param("bulkcrapuninstaller", "check_update.py", "track: github", id="hand-rolled-github-client"),
        pytest.param("msys2", "check_update.py", "track: github", id="github-release-list"),
        pytest.param("restic-backup-gx", "check_update.py", "track: gitlab", id="gitlab-releases"),
        pytest.param("graphviz", "check_update.py", "track: page", id="static-page-scraper-with-a-gitlab-download-url"),
        pytest.param("hwinfo", "check_update.py", "track: page", id="page-scraper"),
        pytest.param("vlc", "check_update.py", "track: index", id="directory-index"),
        pytest.param("python", "check_update.py", "track: index", id="directory-index-with-probe"),
        pytest.param("crystaldiskinfo", "check_update.py", "track: redirect", id="redirect-resolver"),
        pytest.param("zotero", "check_update.py", "track: redirect", id="redirect-resolver-again"),
        pytest.param("firefox", "check_update.py", "track: json", id="vendor-json"),
        pytest.param("julia", "check_update.py", "track: json", id="vendor-json-with-records"),
        pytest.param("autoruns", "check_update.py", "track: last_modified", id="last-modified-header"),
        pytest.param("netio-gui", "check_update.py", "track: rss", id="rss-feed"),
        pytest.param("lockhunter", "rename_exe.py", "prepare: rename", id="rename-hook"),
        pytest.param("volta", "unpack_app.py", "prepare: copy", id="copy-the-shim"),
        pytest.param("systeminformer", "remove_settings.py", "prepare: remove", id="delete-files-hook"),
        pytest.param("firefox", "unpack_app.py", "unpack: 7z, sfx, nsis or tar", id="seven-zip-subdir-hook"),
    ],
)
def test_known_hooks_are_recognised_as_the_service_that_replaces_them(package: str, hook: str, expected: str) -> None:
    """Each real registry hook of a known pattern is hinted with the matching service name."""
    assert expected in services_of(package, hook)


def test_a_hint_for_a_github_hook_names_the_repository_it_reads() -> None:
    """The suggestion for a GitHub client fills in the `owner/name` found in the hook."""
    path = REGISTRY / "bulkcrapuninstaller" / "check_update.py"

    hint = next(h for h in scan_text(path.read_text(encoding="utf-8-sig"), "check_update.py") if h.service == "track: github")

    assert 'repo = "BCUninstaller/Bulk-Crap-Uninstaller"' in hint.suggestion


def test_the_vendor_installer_hook_has_no_hint_and_most_hooks_have_one() -> None:
    """GIMP's installer-running unpack hook is not replaceable by a service (decision D39); every other committed hook is."""
    hinted = [path for path in HOOKS if scan_text(path.read_text(encoding="utf-8-sig"), path.name)]
    unhinted = [path for path in HOOKS if path not in hinted]

    assert len(HOOKS) == 18
    assert len(hinted) == 17
    assert [(path.parts[-2], path.name) for path in unhinted] == [("gimp", "unpack_app.py")]


def test_discovery_patterns_do_not_fire_on_an_unpack_hook_and_hooks_without_patterns_are_silent(tmp_path: Path) -> None:
    """A hook that only imports modules or defines a function with no recognisable work produces no hint."""
    folder = tmp_path / "pkg.local"
    folder.mkdir()
    (folder / "x.py").write_text("PKG_MODULE_API = 2\nimport urllib.request\n\ndef populate(ctx):\n    return 1\n", encoding="utf-8")

    assert scan_directory(tmp_path) == []


def test_pkg_check_reports_a_matching_hook_as_a_warning_with_the_replacement(tmp_path: Path) -> None:
    """A tracker whose module hook is a hand-rolled GitHub client is valid, with a warning that shows the `[track]` to write."""
    tracker = tmp_path / "pkg" / "_check_updates"
    (tracker / "pkg.local").mkdir(parents=True)
    (tracker / "pkg.toml").write_text(
        'format = 2\n[track]\nprovider = "module"\nmodule = "pkg.local/check_update.py"\n[source]\ndownload_url = "${download_url}"\n',
        encoding="utf-8")
    source = (REGISTRY / "bulkcrapuninstaller" / "check_update.py").read_text(encoding="utf-8-sig")
    (tracker / "pkg.local" / "check_update.py").write_text("PKG_MODULE_API = 2\n" + source, encoding="utf-8")

    report = check_directory(tracker)

    assert [error for error in report.errors if "PKG_MODULE_API" in error] == []
    warning = next(text for text in report.warnings if "track: github" in text)
    assert "pkg.local/check_update.py" in warning and 'provider = "github"' in warning
