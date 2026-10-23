"""Cover the user-visible behaviour of ``gupkg-bootstrap.cmd`` / ``gupkg-bootstrap.ps1``.

The wrapper, the real PowerShell script, a real release ZIP built by
``tools/build_standalone.py`` (the ``release_bundle`` fixture), and the installed
launcher are used. ``-SkipInstall`` is passed everywhere, so no test changes the
registry, PATH, or Start Menu, and no network is used. The ``gupkg install`` /
``manager init`` steps that follow placement are covered by the install and
manager tests, and the default source (``RELEASES.md`` and the GitHub asset) by
the manual smoke test, not here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "gupkg-bootstrap.cmd"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the bootstrap script targets Windows PowerShell")


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the bootstrap wrapper, always placing files only, under a temporary root."""
    return subprocess.run(
        [str(BOOTSTRAP), "-Root", str(tmp_path / "opt"), "-SkipInstall", *arguments],
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_verified_release_is_placed_as_a_working_versioned_folder_and_rerun_reuses_it(
    tmp_path: Path, release_bundle
) -> None:
    """A verified release ZIP lands in <root>\\gupkg\\<version>, runs on its own Python, and a rerun is a no-op."""
    digest = release_bundle.sha256.upper()

    first = _run(tmp_path, "-Source", str(release_bundle.zip), "-Sha256", digest)
    placed = next((tmp_path / "opt" / "gupkg").glob("*"))
    second = _run(tmp_path, "-Source", str(release_bundle.zip), "-Sha256", digest)
    version = subprocess.run([str(placed / "gupkg" / "gupkg.cmd"), "--version"], capture_output=True, text=True)

    assert first.returncode == 0, first.stderr
    assert "SHA-256 verified" in first.stdout and "against MANIFEST.sha256" in first.stdout
    assert "reusing it" in second.stdout and second.returncode == 0
    assert placed.name == release_bundle.version
    assert version.stdout.strip() == f"gupkg {release_bundle.version}"


def test_reused_version_folder_is_checked_against_its_manifest(tmp_path: Path, release_bundle) -> None:
    """A rerun refuses an existing version folder whose file was edited, but accepts installation state."""
    digest = release_bundle.sha256.upper()
    _run(tmp_path, "-Source", str(release_bundle.zip), "-Sha256", digest)
    placed = next((tmp_path / "opt" / "gupkg").glob("*"))
    (placed / "App").mkdir()
    (placed / "App" / "state.txt").write_text("x", encoding="utf-8")
    (placed / "gupkg" / "gupkg-config.toml").write_text("x", encoding="utf-8")
    clean = _run(tmp_path, "-Source", str(release_bundle.zip), "-Sha256", digest)
    (placed / "gupkg" / "cli.py").write_text("# edited\n", encoding="utf-8")

    damaged = _run(tmp_path, "-Source", str(release_bundle.zip), "-Sha256", digest)

    assert clean.returncode == 0 and "reusing it" in clean.stdout
    assert damaged.returncode == 1 and "1 modified" in damaged.stderr


def test_archive_entry_that_escapes_the_destination_is_rejected(tmp_path: Path, release_bundle) -> None:
    """A ZIP with a '..' entry stops the bootstrap before anything is written outside its work folder."""
    evil = tmp_path / "evil.zip"
    digest = _derive(release_bundle.zip, evil, tmp_path)

    result = _run(tmp_path, "-Source", str(evil), "-Sha256", digest)

    assert result.returncode == 1
    assert "unsafe path" in result.stderr
    assert not (tmp_path / "planted.txt").exists() and not (tmp_path / "opt").exists()


def _derive(source: Path, target: Path, tmp_path: Path) -> str:
    """Copy a ZIP and add an entry whose name climbs out of the extraction folder; return its upper-case SHA-256."""
    import hashlib
    import zipfile

    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as copy:
        for entry in original.infolist():
            copy.writestr(entry.filename, original.read(entry))
        copy.writestr("../../../../../../../../../../../../planted.txt", b"x")
    return hashlib.sha256(target.read_bytes()).hexdigest().upper()


def test_checksum_mismatch_stops_before_anything_is_placed(tmp_path: Path, release_bundle) -> None:
    """A wrong -Sha256 fails with a clear message and creates no install folder."""
    result = _run(tmp_path, "-Source", str(release_bundle.zip), "-Sha256", "0" * 64)

    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert not (tmp_path / "opt").exists()


def test_missing_sha256_for_a_given_zip_warns_but_still_installs(tmp_path: Path, release_bundle) -> None:
    """A -Source ZIP without -Sha256 prints the unverified-download warning and is still checked against its manifest."""
    result = _run(tmp_path, "-Source", str(release_bundle.zip))

    assert result.returncode == 0, result.stderr
    assert "not verified" in result.stdout + result.stderr
    assert "against MANIFEST.sha256" in result.stdout


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        pytest.param({"replace": ("gupkg/cli.py", b"# edited\n")}, "1 modified", id="modified-file"),
        pytest.param({"drop": ("gupkg/tls.py",)}, "1 missing", id="missing-file"),
        pytest.param({"add": {"gupkg/planted.py": b"x"}}, "1 extra", id="extra-file"),
    ],
)
def test_release_that_differs_from_its_manifest_is_rejected_before_placement(
    tmp_path: Path, release_bundle, derive_zip, variant: dict, message: str
) -> None:
    """A modified, missing or extra file makes the bootstrap stop with the count, and nothing is installed."""
    damaged = tmp_path / "damaged.zip"
    digest = derive_zip(release_bundle.zip, damaged, **variant).upper()

    result = _run(tmp_path, "-Source", str(damaged), "-Sha256", digest)

    assert result.returncode == 1
    assert "does not match its MANIFEST.sha256" in result.stderr and message in result.stderr
    assert not (tmp_path / "opt").exists()


def test_payload_without_a_bundled_python_is_rejected(tmp_path: Path, release_bundle, derive_zip) -> None:
    """A ZIP that lacks gupkg\\python\\python.exe is not a release and is refused with a pointer to the release ZIP."""
    stripped = tmp_path / "stripped.zip"
    digest = derive_zip(release_bundle.zip, stripped, drop=("gupkg/python/python.exe",)).upper()

    result = _run(tmp_path, "-Source", str(stripped), "-Sha256", digest)

    assert result.returncode == 1
    assert "not a self-contained gupkg release" in result.stderr
    assert not (tmp_path / "opt").exists()


def test_system_scope_requires_an_elevated_shell(tmp_path: Path, release_bundle) -> None:
    """System scope from a non-elevated shell explains how to proceed instead of failing midway."""
    principal = subprocess.run(["net", "session"], capture_output=True, text=True)
    if principal.returncode == 0:
        pytest.skip("the test process is elevated")

    result = _run(tmp_path, "-Scope", "system", "-Source", str(release_bundle.zip))

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
