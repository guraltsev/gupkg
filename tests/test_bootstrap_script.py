"""Cover the user-visible behaviour of ``gupkg-bootstrap.cmd`` / ``gupkg-bootstrap.ps1``.

Behavior covered: the thin bootstrap verifies the release ZIP against ``-Sha256`` before unpacking anything, unpacks it safely
(an entry that climbs out of the destination stops it), finds the version directory with its ``install.cmd``, and with
``-SkipInstall`` stops there; the unpacked release runs on its own Python; user scope refuses to run elevated and system scope
refuses to run without elevation; a system root that is not admin-only (a ``C:\\opt`` that a standard user created) is
recognised and refused, and one that is admin-only is accepted; a source that does not exist is reported plainly. A missing
``gupkg-bootstrap.ps1`` next to the ``.cmd`` is downloaded after confirmation.

Real: the wrapper, the real PowerShell script and a real release ZIP built by ``tools/build_standalone.py`` (the
``release_bundle`` fixture). ``-SkipInstall`` is passed wherever the script would run ``install.cmd``, so no test changes the
registry, ``PATH`` or Start Menu, and no network is used. Out of scope: the install itself (``install.cmd`` and gupkg, covered by
the engine tests and manual smoke tests 033-2 to 033-6), the default source (``RELEASES.md`` and the GitHub asset) and creating
``C:\\opt`` (needs an elevated shell: manual smoke test 033-3).
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
SCRIPT = REPO / "gupkg-bootstrap.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the bootstrap script targets Windows PowerShell")


def _elevated() -> bool:
    """Return whether this test process holds an elevated administrator token (``fltmc`` needs one)."""
    return subprocess.run([os.environ["SystemRoot"] + "\\System32\\fltmc.exe"], capture_output=True, check=False).returncode == 0


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the bootstrap wrapper in user scope, unpacking into ``tmp_path/stage`` and never running install.cmd."""
    return subprocess.run(
        [str(BOOTSTRAP), "-Scope", "user", "-Stage", str(tmp_path / "stage"), "-SkipInstall", *arguments],
        capture_output=True, text=True, timeout=300,
    )


@pytest.mark.skipif(_elevated(), reason="user scope refuses an elevated shell")
def test_verified_release_is_unpacked_with_its_install_cmd_and_runs_on_its_own_python(tmp_path: Path, release_bundle) -> None:
    """A verified release ZIP is unpacked as gupkg/<version> with install.cmd and a bundled gupkg that runs; install.cmd is not run."""
    digest = release_bundle.sha256.upper()

    result = _run(tmp_path, "-Source", str(release_bundle.zip), "-Sha256", digest)

    version_dir = tmp_path / "stage" / "gupkg" / release_bundle.version
    assert result.returncode == 0, result.stderr
    assert "SHA-256 verified" in result.stdout and f"Version directory: {version_dir}" in result.stdout
    assert "install.cmd" in result.stdout and "--mode user" in result.stdout
    assert (version_dir / "install.cmd").is_file() and (version_dir / "pkg.toml").is_file()
    version = subprocess.run([str(version_dir / "gupkg" / "gupkg.cmd"), "--version"], capture_output=True, text=True)
    assert version.stdout.strip() == f"gupkg {release_bundle.version}"


@pytest.mark.skipif(_elevated(), reason="user scope refuses an elevated shell")
def test_archive_entry_that_escapes_the_destination_is_rejected(tmp_path: Path, release_bundle) -> None:
    """A ZIP with a '..' entry stops the bootstrap before anything is written outside its work folder."""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(release_bundle.zip) as original, zipfile.ZipFile(evil, "w") as copy:
        for entry in original.infolist():
            copy.writestr(entry.filename, original.read(entry))
        copy.writestr("../../../../../../../../../../../../planted.txt", b"x")
    digest = hashlib.sha256(evil.read_bytes()).hexdigest().upper()

    result = _run(tmp_path, "-Source", str(evil), "-Sha256", digest)

    assert result.returncode == 1
    assert "unsafe path" in result.stderr
    assert not (tmp_path / "planted.txt").exists()


@pytest.mark.skipif(_elevated(), reason="user scope refuses an elevated shell")
def test_checksum_mismatch_stops_before_anything_is_unpacked(tmp_path: Path, release_bundle) -> None:
    """A wrong -Sha256 fails with a clear message and unpacks nothing."""
    result = _run(tmp_path, "-Source", str(release_bundle.zip), "-Sha256", "0" * 64)

    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert not (tmp_path / "stage").exists()


@pytest.mark.skipif(_elevated(), reason="user scope refuses an elevated shell")
def test_missing_sha256_for_a_given_zip_warns_but_still_unpacks(tmp_path: Path, release_bundle) -> None:
    """A -Source ZIP without -Sha256 prints the unverified-download warning and is still unpacked (gupkg checks the tree itself)."""
    result = _run(tmp_path, "-Source", str(release_bundle.zip))

    assert result.returncode == 0, result.stderr
    assert "not verified" in result.stdout + result.stderr
    assert (tmp_path / "stage" / "gupkg" / release_bundle.version / "install.cmd").is_file()


@pytest.mark.skipif(_elevated(), reason="user scope refuses an elevated shell")
def test_a_zip_without_an_install_launcher_is_not_a_distribution(tmp_path: Path, release_bundle, derive_zip) -> None:
    """A ZIP whose version directory has no install.cmd is refused, naming what was missing."""
    stripped = tmp_path / "stripped.zip"
    digest = derive_zip(release_bundle.zip, stripped, drop=("install.cmd",)).upper()

    result = _run(tmp_path, "-Source", str(stripped), "-Sha256", digest)

    assert result.returncode == 1
    assert "No version directory" in result.stderr


@pytest.mark.skipif(_elevated(), reason="user scope refuses an elevated shell")
def test_a_folder_source_is_used_where_it_is_in_user_scope(tmp_path: Path, release_bundle) -> None:
    """An extracted distribution folder is used in place: its version directory is found and nothing is copied or run."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        archive.extractall(tmp_path / "extracted")

    result = _run(tmp_path, "-Source", str(tmp_path / "extracted"))

    assert result.returncode == 0, result.stderr
    assert f"Version directory: {tmp_path / 'extracted' / 'gupkg' / release_bundle.version}" in result.stdout
    assert not (tmp_path / "stage").exists()


def test_system_scope_requires_an_elevated_shell(tmp_path: Path, release_bundle) -> None:
    """System scope from a non-elevated shell explains how to proceed instead of failing midway."""
    if _elevated():
        pytest.skip("the test process is elevated")

    result = subprocess.run(
        [str(BOOTSTRAP), "-Scope", "system", "-Root", str(tmp_path / "opt"), "-Source", str(release_bundle.zip)],
        capture_output=True, text=True, timeout=300,
    )

    assert result.returncode == 1
    assert "elevated PowerShell" in result.stderr
    assert not (tmp_path / "opt").exists()


def test_user_scope_refuses_an_elevated_shell(tmp_path: Path, release_bundle) -> None:
    """User scope never runs as administrator: gupkg does not act on a user root elevated (D31)."""
    if not _elevated():
        pytest.skip("the test process is not elevated")

    result = _run(tmp_path, "-Source", str(release_bundle.zip))

    assert result.returncode == 1 and "not elevated" in result.stderr


def _dot_source(tmp_path: Path, command: str) -> str:
    """Define the functions of the script (dot-sourcing runs nothing) and return the output of *command*."""
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", f". '{SCRIPT}'; {command}"],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_a_root_that_a_standard_user_created_is_not_admin_only(tmp_path: Path) -> None:
    """A folder the current user owns (what a squatted C:\\opt is) is reported with a reason, so the system bootstrap refuses it."""
    squatted = tmp_path / "opt"
    squatted.mkdir()

    problem = _dot_source(tmp_path, f"Get-AdminOnlyProblem '{squatted}'")

    assert problem and ("owner" in problem or "can write" in problem)


def test_an_admin_only_folder_is_accepted(tmp_path: Path) -> None:
    """The Windows folder (owned by TrustedInstaller, writable by administrators only) has no problem."""
    problem = _dot_source(tmp_path, f"$p = Get-AdminOnlyProblem '{os.environ['SystemRoot']}'; if ($p) {{ $p }} else {{ 'none' }}")

    assert problem == "none"


def test_missing_source_is_reported_plainly(tmp_path: Path) -> None:
    """A source that is neither a URL, a ZIP, nor a folder is rejected with a clear error."""
    if _elevated():
        pytest.skip("user scope refuses an elevated shell")

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
