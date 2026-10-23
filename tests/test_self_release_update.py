"""Cover gupkg updating itself from the release table in RELEASES.md.

Behavior covered: the gupkg package finds the newest row of RELEASES.md, downloads
the release ZIP it names, verifies the ZIP against the row's SHA-256 and the
unpacked tree against its own ``MANIFEST.sha256``, and stages a complete new
version tree whose runtime comes only from the ZIP; a row without a digest, a ZIP
with the wrong digest, a ZIP that does not match its manifest, a ZIP built from
another commit, and a 1.x release all fail and leave the active version unchanged.

Real: a release ZIP built by ``tools/build_standalone.py`` (the ``release_bundle``
fixture), the installed version tree, the update coordinator, hook loading, and
staging on disk. Stood in by ``FakeSystem``: HTTP (the RELEASES.md and release ZIP
URLs are fixtures). Out of scope:
shortcut/PATH activation, which ``test_update_lifecycle`` covers, and GitHub itself.
"""

from __future__ import annotations

import importlib.util
import shutil
import zipfile
from pathlib import Path

import pytest

from gupkg import gupkg as workflows
from gupkg.integrity import verify_version_directory
from gupkg.system.fake import FakeSystem

REPO = Path(__file__).resolve().parents[1]
OLD_VERSION = "0.0.1"
RELEASES_URL = "https://raw.githubusercontent.com/guraltsev/gupkg/main/RELEASES.md"


def _installed(tmp_path: Path, release_bundle) -> Path:
    """Install the release ZIP's tree as an old active version, plus marker files of an old install."""
    extract = tmp_path / "extract"
    with zipfile.ZipFile(release_bundle.zip) as archive:
        archive.extractall(extract)
    tree = next(extract.iterdir())
    installed = tmp_path / "gupkg" / OLD_VERSION
    installed.parent.mkdir()
    shutil.move(str(tree), str(installed))
    (installed / "gupkg" / "python" / "old_runtime_marker.txt").write_text("old runtime", encoding="utf-8")
    (installed / "gupkg" / "gupkg-config.toml").write_text('mode = "manager"\n', encoding="utf-8")
    return installed


def _releases_text(version: str, commit: str, digest: str | None) -> bytes:
    """Return a RELEASES.md body listing one release, with or without a digest."""
    shown = digest or "-"
    return (
        "| Version | Commit | Date | SHA-256 | Notes |\n|---|---|---|---|---|\n"
        f"| {version} | `{commit}` | 2026-01-01 | {shown} | test |\n"
    ).encode()


def _update(system: FakeSystem, installed: Path, releases: bytes, download: bytes | None, version: str = ""):
    """Stage the update with RELEASES.md and (when given) the release ZIP of *version* served by *system*."""
    system.add_url(RELEASES_URL, releases)
    if download is not None:
        system.add_url(
            f"https://github.com/guraltsev/gupkg/releases/download/v{version}/gupkg-{version}-win-amd64.zip",
            download,
        )
    return workflows.download_package_update(installed, system)


def test_update_stages_the_listed_release_zip_as_a_complete_verified_version(
    tmp_path: Path, release_bundle, system: FakeSystem
) -> None:
    """A newer row yields a version folder from the ZIP: new runtime only, kept configuration, exact manifest."""
    installed = _installed(tmp_path, release_bundle)
    releases = _releases_text(release_bundle.version, release_bundle.commit, release_bundle.sha256)

    result = _update(system, installed, releases, release_bundle.zip.read_bytes(), release_bundle.version)

    staged = tmp_path / "gupkg" / release_bundle.version
    assert result.ok, result.errors
    assert result.status == "downloaded"
    assert (staged / "gupkg" / "python" / "python.exe").is_file()
    assert not (staged / "gupkg" / "python" / "old_runtime_marker.txt").exists()
    assert (staged / "gupkg" / "gupkg-config.toml").read_text(encoding="utf-8") == 'mode = "manager"\n'
    assert verify_version_directory(staged).ok
    assert (staged / "App" / "RELEASE.txt").is_file()
    assert (installed / "gupkg" / "python" / "old_runtime_marker.txt").is_file()


def test_update_reports_current_when_the_table_lists_no_newer_release(
    tmp_path: Path, release_bundle, system: FakeSystem
) -> None:
    """The newest row equal to the installed version is not an update."""
    installed = _installed(tmp_path, release_bundle)

    result = _update(system, installed, _releases_text(OLD_VERSION, release_bundle.commit, "a" * 64), None)

    assert result.ok, result.errors
    assert result.status == "current"
    assert not (tmp_path / "gupkg" / release_bundle.version).exists()


def test_update_refuses_a_zip_whose_digest_differs_from_the_listed_one(
    tmp_path: Path, release_bundle, system: FakeSystem
) -> None:
    """A download that does not hash to the row's SHA-256 is never unpacked or staged."""
    installed = _installed(tmp_path, release_bundle)
    releases = _releases_text(release_bundle.version, release_bundle.commit, "0" * 64)

    result = _update(system, installed, releases, release_bundle.zip.read_bytes(), release_bundle.version)

    assert not result.ok
    assert any("checksum" in error for error in result.errors)
    assert not (tmp_path / "gupkg" / release_bundle.version).exists()


def test_update_refuses_a_zip_that_does_not_match_its_manifest(
    tmp_path: Path, release_bundle, derive_zip, system: FakeSystem
) -> None:
    """A ZIP with the digest the table lists but a file that differs from its manifest is refused."""
    installed = _installed(tmp_path, release_bundle)
    tampered = tmp_path / "tampered.zip"
    digest = derive_zip(release_bundle.zip, tampered, replace=("gupkg/cli.py", b"# tampered\n"))
    releases = _releases_text(release_bundle.version, release_bundle.commit, digest)

    result = _update(system, installed, releases, tampered.read_bytes(), release_bundle.version)

    assert not result.ok
    assert any("MANIFEST.sha256" in error for error in result.errors)
    assert not (tmp_path / "gupkg" / release_bundle.version).exists()
    assert (installed / "gupkg" / "python" / "old_runtime_marker.txt").is_file()


def test_update_refuses_a_zip_built_from_another_commit(
    tmp_path: Path, release_bundle, system: FakeSystem
) -> None:
    """A ZIP whose recorded commit differs from the listed one is never staged."""
    installed = _installed(tmp_path, release_bundle)
    releases = _releases_text(release_bundle.version, "b" * 40, release_bundle.sha256)

    result = _update(system, installed, releases, release_bundle.zip.read_bytes(), release_bundle.version)

    assert not result.ok
    assert any("commit" in error for error in result.errors)
    assert not (tmp_path / "gupkg" / release_bundle.version).exists()


def test_update_fails_for_a_newest_row_without_a_digest(tmp_path: Path, release_bundle, system: FakeSystem) -> None:
    """A release row with no SHA-256 is an error, not something the updater skips or trusts."""
    installed = _installed(tmp_path, release_bundle)

    result = _update(system, installed, _releases_text(release_bundle.version, release_bundle.commit, None), None)

    assert not result.ok
    assert any("without a SHA-256" in error for error in result.errors)


def test_update_to_a_1x_release_installs_nothing_and_names_the_bootstrap(
    tmp_path: Path, release_bundle, system: FakeSystem
) -> None:
    """A major version 1 release is not installed by this updater; the message names gupkg-bootstrap.cmd."""
    installed = _installed(tmp_path, release_bundle)

    result = _update(system, installed, _releases_text("1.0.0", "c" * 40, "d" * 64), None)

    assert not result.ok
    assert any("gupkg-bootstrap.cmd" in error for error in result.errors)
    assert sorted(path.name for path in (tmp_path / "gupkg").iterdir() if path.is_dir() and not path.name.startswith(".")) == [OLD_VERSION]


def test_check_update_returns_the_asset_url_and_digest_of_the_newest_row(system: FakeSystem) -> None:
    """The hook turns the newest RELEASES.md row into a candidate carrying the release asset URL and SHA-256."""
    spec = importlib.util.spec_from_file_location("check_update_under_test", REPO / "src" / "pkg.local" / "check_update.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    digest = "ab" * 32
    table = (
        "| Version | Commit | Date | SHA-256 | Notes |\n|---|---|---|---|---|\n"
        f"| 0.19.0 | `{'1' * 40}` | 2026-02-01 | {digest} | newer |\n"
        f"| 0.18.0 | `{'2' * 40}` | 2026-01-01 | {'cd' * 32} | older |\n"
        f"| 0.17.0 | `{'3' * 40}` | 2025-12-01 | - | before digests |\n"
    ).encode()

    system.add_url(RELEASES_URL, table)

    candidate = hook.check_update({"current": {"version": "0.18.0"}, "fetch_text": system.fetch_text})

    assert candidate["version"] == "0.19.0"
    assert candidate["sha256"] == digest
    assert candidate["url"] == "https://github.com/guraltsev/gupkg/releases/download/v0.19.0/gupkg-0.19.0-win-amd64.zip"
    assert hook.check_update({"current": {"version": "0.19.0"}, "fetch_text": system.fetch_text}) is None


def test_check_update_rejects_a_release_row_without_a_digest(system: FakeSystem) -> None:
    """A newest row marked '-' makes the check fail with an explicit message."""
    spec = importlib.util.spec_from_file_location("check_update_under_test", REPO / "src" / "pkg.local" / "check_update.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    system.add_url(RELEASES_URL, _releases_text("0.19.0", "1" * 40, None))

    with pytest.raises(RuntimeError, match="without a SHA-256"):
        hook.check_update({"current": {"version": "0.18.0"}, "fetch_text": system.fetch_text})


def test_check_update_without_fetch_text_asks_for_a_newer_gupkg() -> None:
    """A context from an older engine (no ``fetch_text``) fails with a clear message instead of a KeyError."""
    spec = importlib.util.spec_from_file_location("check_update_under_test", REPO / "src" / "pkg.local" / "check_update.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    with pytest.raises(RuntimeError, match="too old for this package; update gupkg"):
        hook.check_update({"current": {"version": "0.18.0"}})
