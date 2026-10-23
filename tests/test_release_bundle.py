"""Cover the self-contained release ZIP and the integrity data computed at build time.

Behavior covered: ``tools/build_standalone.py`` writes ``gupkg-<version>-win-amd64.zip``
with a ``MANIFEST.sha256`` that lists every other file with its correct digest,
a ``RELEASE.txt`` that records the version and commit, a runtime configured to
import from its own folder, and nothing that belongs to one install (a manager
configuration, the source dump); the build stops when the CPython archive does not
match the pinned digest; an installed version is checked against its manifest
(modified, missing and extra files are reported, installation state is not), and
``gupkg manager self status`` run from the bundle reports an edited file.

Real: the build tool run as a subprocess, the ZIP it writes, the bundle's own
launcher and bundled Python for the ``self status`` test. Mocked: nothing. The
dependencies are not installed (``--skip-deps``), so no network is used. Out of
scope: the bootstrap script and self-update, covered by their own modules.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from gupkg.integrity import verify_version_directory

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "tools" / "build_standalone.py"


@pytest.fixture
def installed(tmp_path: Path, release_bundle) -> Path:
    """The release ZIP extracted as an installed version directory (the folder with pkg.toml)."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        archive.extractall(tmp_path / "unpacked")
    return next((tmp_path / "unpacked").iterdir())


def test_manifest_lists_every_other_file_with_its_correct_digest(release_bundle) -> None:
    """MANIFEST.sha256 has one sorted '<sha256>  <path>' line per file except itself, each digest correct."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        top = archive.namelist()[0].split("/")[0]
        manifest = archive.read(f"{top}/gupkg/MANIFEST.sha256").decode("utf-8").splitlines()
        files = {
            name.removeprefix(f"{top}/"): hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if not name.endswith("/") and name != f"{top}/gupkg/MANIFEST.sha256"
        }

    listed = [tuple(line.split("  ", 1)) for line in manifest]
    assert [path for _, path in listed] == sorted(files)
    assert {path: digest for digest, path in listed} == files


def test_release_zip_is_named_for_the_version_and_records_its_commit(release_bundle) -> None:
    """The ZIP carries RELEASE.txt with the version and the build commit, and its digest is published beside it."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        top = archive.namelist()[0].split("/")[0]
        release_note = archive.read(f"{top}/gupkg/RELEASE.txt").decode("utf-8")
        python_path = archive.read(f"{top}/gupkg/python/python312._pth").decode("ascii")
        names = archive.namelist()

    assert top == f"gupkg-{release_bundle.version}"
    assert release_note == f"gupkg {release_bundle.version}\ncommit {release_bundle.commit}\n"
    assert "Lib/site-packages" in python_path
    assert f"{top}/gupkg/python/python.exe" in names
    assert not any(name.endswith(("gupkg-config.toml", "digest.txt")) for name in names)
    sidecar = release_bundle.zip.with_name(release_bundle.zip.name + ".sha256").read_text(encoding="ascii")
    assert sidecar.split()[0] == release_bundle.sha256


def test_build_stops_when_the_runtime_archive_does_not_match_the_pinned_digest(tmp_path: Path) -> None:
    """A CPython archive whose SHA-256 differs from the expected one stops the build and writes no ZIP."""
    runtime = tmp_path / "runtime.zip"
    with zipfile.ZipFile(runtime, "w") as archive:
        archive.writestr("python312._pth", "python312.zip\n")
    output = tmp_path / "out.zip"

    result = subprocess.run(
        [sys.executable, str(BUILD), "--skip-deps", "--runtime", str(runtime), "--runtime-sha256", "0" * 64,
         "--commit", "a" * 40, "--output", str(output)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not match" in result.stderr
    assert not output.exists()


def test_untouched_version_directory_matches_its_manifest(installed: Path) -> None:
    """A freshly unpacked release has no modified, missing or extra file."""
    assert verify_version_directory(installed).ok


def test_installation_state_is_not_reported_as_extra(installed: Path) -> None:
    """The App, Icons and Shortcuts folders and the per-version manager configuration are install state."""
    (installed / "App").mkdir()
    (installed / "App" / "RELEASE.txt").write_text("marker", encoding="utf-8")
    (installed / "Icons").mkdir()
    (installed / "Shortcuts").mkdir()
    (installed / "Shortcuts" / "tool.lnk").write_text("x", encoding="utf-8")
    (installed / "gupkg" / "gupkg-config.toml").write_text('mode = "manager"\n', encoding="utf-8")
    (installed / "gupkg" / "__pycache__").mkdir()
    (installed / "gupkg" / "__pycache__" / "cli.cpython-312.pyc").write_bytes(b"\0")

    assert verify_version_directory(installed).ok


def test_modified_missing_and_extra_files_are_listed(installed: Path) -> None:
    """One edited, one deleted and one added file are each reported under their own heading."""
    (installed / "gupkg" / "cli.py").write_text("# edited\n", encoding="utf-8")
    (installed / "gupkg" / "tls.py").unlink()
    (installed / "gupkg" / "planted.py").write_text("x", encoding="utf-8")

    report = verify_version_directory(installed)

    assert not report.ok
    assert report.modified == ("gupkg/cli.py",)
    assert report.missing == ("gupkg/tls.py",)
    assert report.extra == ("gupkg/planted.py",)


def test_version_directory_without_a_manifest_is_reported(installed: Path) -> None:
    """A version directory that has no MANIFEST.sha256 is not healthy, and says so."""
    (installed / "gupkg" / "MANIFEST.sha256").unlink()

    report = verify_version_directory(installed)

    assert not report.ok and not report.manifest_found
    assert "MANIFEST.sha256 is missing" in report.problems()[0]


def _elevated() -> bool:
    """Return whether this test process runs with an administrator token."""
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


@pytest.mark.skipif(os.name != "nt" or _elevated(), reason="runs the bundle's launcher; an elevated run demands admin-only folders")
def test_manager_self_status_reports_the_edited_file(installed: Path, tmp_path: Path) -> None:
    """`gupkg manager self status` is healthy on the bundle, and names the file after one is edited."""
    config = tmp_path / "gupkg-config.toml"
    (tmp_path / "system").mkdir()
    (tmp_path / "user").mkdir()
    config.write_text(
        'mode = "manager"\nschema_version = 2\n\n'
        f"[packages]\nsystem = '{tmp_path / 'system'}'\nuser = '{tmp_path / 'user'}'\n\n"
        f"[bin]\nsystem = '{tmp_path / 'system-bin'}'\nuser = '{tmp_path / 'user-bin'}'\n\n"
        f"[registry]\ncache = '{tmp_path / 'registry'}'\n\n[shims]\nlinkage = 'dynamic'\n",
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items() if key != "GUPKG_FORCE_ELEVATED"}
    command = [str(installed / "gupkg" / "gupkg.cmd"), "manager", "--config", str(config), "self", "status"]

    healthy = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120)
    (installed / "gupkg" / "README.md").write_text("# edited\n", encoding="utf-8")
    damaged = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120)

    assert "self.runtime_healthy: True" in healthy.stdout and healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert damaged.returncode == 3
    assert "modified: gupkg/README.md" in damaged.stderr
    assert "Reinstall gupkg from a release ZIP" in damaged.stderr
