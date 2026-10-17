"""Cover the user-visible behaviour of ``gupkg-bootstrap.cmd`` / ``gupkg-bootstrap.ps1``.

The wrapper, the real PowerShell script, a real ZIP of this repository's
``src`` folder, and the installed launcher are used. ``-SkipInstall`` is passed
everywhere, so no test changes the registry, PATH, or Start Menu, and no network
is used. The ``gupkg install`` / ``manager init`` steps that follow placement
are covered by the install and manager tests, not here.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "gupkg-bootstrap.cmd"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the bootstrap script targets Windows PowerShell")


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A GitHub-style repository archive (``pkg-main/src/...``) built from this checkout."""
    path = tmp_path_factory.mktemp("bootstrap") / "repo.zip"
    src = REPO / "src"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipped:
        for file in src.rglob("*"):
            relative = file.relative_to(src)
            if file.is_file() and "__pycache__" not in relative.parts and relative.parts[:2] != ("gupkg", "python"):
                zipped.write(file, Path("pkg-main") / "src" / relative)
        zipped.writestr("pkg-main/pkgs/other/readme.txt", "not part of gupkg")
    return path


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the bootstrap wrapper, always placing files only, under a temporary root."""
    return subprocess.run(
        [str(BOOTSTRAP), "-Root", str(tmp_path / "opt"), "-SkipInstall", *arguments],
        capture_output=True,
        text=True,
        timeout=100,
    )


def test_verified_archive_is_placed_as_a_working_versioned_folder_and_rerun_reuses_it(
    tmp_path: Path, archive: Path
) -> None:
    """A verified archive lands in <root>\\gupkg\\v<version>, runs, and a rerun is a no-op."""
    digest = hashlib.sha256(archive.read_bytes()).hexdigest().upper()

    first = _run(tmp_path, "-Source", str(archive), "-Sha256", digest)
    placed = next((tmp_path / "opt" / "gupkg").glob("v*"))
    second = _run(tmp_path, "-Source", str(archive), "-Sha256", digest)
    version = subprocess.run([str(placed / "gupkg.cmd"), "--version"], capture_output=True, text=True)

    assert first.returncode == 0 and "SHA-256 verified" in first.stdout
    assert "reusing it" in second.stdout and second.returncode == 0
    assert version.stdout.strip() == f"gupkg {placed.name[1:]}"
    assert not (placed / "pkgs").exists()


def test_checksum_mismatch_stops_before_anything_is_placed(tmp_path: Path, archive: Path) -> None:
    """A wrong -Sha256 fails with a clear message and creates no install folder."""
    result = _run(tmp_path, "-Source", str(archive), "-Sha256", "0" * 64)

    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert not (tmp_path / "opt").exists()


def test_system_scope_requires_an_elevated_shell(tmp_path: Path, archive: Path) -> None:
    """System scope from a non-elevated shell explains how to proceed instead of failing midway."""
    principal = subprocess.run(
        ["net", "session"], capture_output=True, text=True
    )
    if principal.returncode == 0:
        pytest.skip("the test process is elevated")

    result = _run(tmp_path, "-Scope", "system", "-Source", str(archive))

    assert result.returncode == 1
    assert "elevated PowerShell" in result.stderr


def test_missing_source_is_reported_plainly(tmp_path: Path) -> None:
    """A source that is neither a URL, a ZIP, nor a folder is rejected with a clear error."""
    result = _run(tmp_path, "-Source", str(tmp_path / "nope.zip"))

    assert result.returncode == 1
    assert "Source not found" in result.stderr


def _lone_cmd(tmp_path: Path) -> Path:
    """Copy only gupkg-bootstrap.cmd into an empty folder, as a user who downloaded just that file."""
    folder = tmp_path / "download"
    folder.mkdir()
    shutil.copy2(BOOTSTRAP, folder / "gupkg-bootstrap.cmd")
    return folder / "gupkg-bootstrap.cmd"


def _fake_remote_script(tmp_path: Path) -> str:
    """Write a stand-in gupkg-bootstrap.ps1 that echoes its arguments, and return its file URL."""
    script = tmp_path / "remote" / "gupkg-bootstrap.ps1"
    script.parent.mkdir()
    script.write_text("param([string]$Marker)\nWrite-Output \"ran with marker $Marker\"\n", encoding="utf-8")
    return script.as_uri()


def test_missing_script_is_downloaded_after_confirmation_and_then_run(tmp_path: Path) -> None:
    """Answering yes downloads the script next to the .cmd, runs it with the same arguments, and keeps it."""
    launcher = _lone_cmd(tmp_path)
    environment = {**os.environ, "GUPKG_BOOTSTRAP_URL": _fake_remote_script(tmp_path)}
    environment.pop("GUPKG_BOOTSTRAP_YES", None)

    result = subprocess.run([str(launcher), "-Marker", "abc"], input="y\n", capture_output=True, text=True, env=environment, timeout=100)

    assert result.returncode == 0, result.stderr
    assert "was not found next to this file" in result.stdout
    assert "ran with marker abc" in result.stdout
    assert (launcher.parent / "gupkg-bootstrap.ps1").is_file()


def test_declining_the_download_runs_nothing_and_saves_nothing(tmp_path: Path) -> None:
    """Answering no cancels with an error and leaves the folder without the script."""
    launcher = _lone_cmd(tmp_path)
    environment = {**os.environ, "GUPKG_BOOTSTRAP_URL": _fake_remote_script(tmp_path)}
    environment.pop("GUPKG_BOOTSTRAP_YES", None)

    result = subprocess.run([str(launcher)], input="n\n", capture_output=True, text=True, env=environment, timeout=100)

    assert result.returncode == 1
    assert "Cancelled" in result.stderr
    assert not (launcher.parent / "gupkg-bootstrap.ps1").exists()


def test_yes_variable_downloads_without_asking(tmp_path: Path) -> None:
    """GUPKG_BOOTSTRAP_YES=1 skips the question, so unattended runs work."""
    launcher = _lone_cmd(tmp_path)
    environment = {**os.environ, "GUPKG_BOOTSTRAP_URL": _fake_remote_script(tmp_path), "GUPKG_BOOTSTRAP_YES": "1"}

    result = subprocess.run([str(launcher), "-Marker", "z"], capture_output=True, text=True, env=environment, timeout=100, stdin=subprocess.DEVNULL)

    assert result.returncode == 0, result.stderr
    assert "ran with marker z" in result.stdout


def test_failed_download_leaves_no_partial_file(tmp_path: Path) -> None:
    """An unreachable address reports the failure and does not leave a half-written script."""
    launcher = _lone_cmd(tmp_path)
    environment = {**os.environ, "GUPKG_BOOTSTRAP_URL": (tmp_path / "missing.ps1").as_uri(), "GUPKG_BOOTSTRAP_YES": "1"}

    result = subprocess.run([str(launcher)], capture_output=True, text=True, env=environment, timeout=100, stdin=subprocess.DEVNULL)

    assert result.returncode == 1
    assert "Could not download" in result.stderr
    assert list(launcher.parent.glob("gupkg-bootstrap.ps1*")) == []
