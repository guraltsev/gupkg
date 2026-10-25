"""Cover ``tools/release.py``: publish the release ZIP and record its SHA-256 in RELEASES.md.

Behavior covered: a release of a clean tree with a ZIP built from ``HEAD`` tags the
commit, uploads the ZIP as the asset of release ``v<version>``, and adds the row
``| version | commit | date | sha256 | notes |`` that carries the digest of the
uploaded file; a dirty tree, an already listed version, and a ZIP built from
another commit or for another version are refused before anything is tagged,
uploaded or recorded.

Real: Git (a temporary repository and a temporary bare remote), the ZIP, and the
script. Mocked: the GitHub CLI (a stand-in ``gh.cmd`` that records its arguments).
Out of scope: building the ZIP (``test_release_bundle.py``) and GitHub itself.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

import release

TABLE = "| Version | Commit | Date | SHA-256 | Notes |\n|---|---|---|---|---|\n| 0.0.9 | `{}` | 2026-01-01 | - | old |\n"


def _git(repo: Path, *arguments: str) -> str:
    """Run Git in *repo* and return its stripped output."""
    return subprocess.run(["git", *arguments], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A committed repository laid out like gupkg: version 0.1.0 and a RELEASES.md table."""
    repo = tmp_path / "repo"
    (repo / "src" / "gupkg").mkdir(parents=True)
    (repo / "src" / "gupkg" / "_version.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (repo / "RELEASES.md").write_text(TABLE.format("0" * 40), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "start")
    return repo


def _release_zip(path: Path, *, version: str, commit: str) -> Path:
    """Write a stand-in release ZIP whose RELEASE.txt records *version* and *commit*."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"gupkg-{version}/gupkg/RELEASE.txt", f"gupkg {version}\ncommit {commit}\n")
    return path


def test_release_tags_the_commit_and_records_the_zip_digest(repository: Path, tmp_path: Path) -> None:
    """Without publishing, the row carries the ZIP's SHA-256 and the released commit, and the commit is tagged."""
    head = _git(repository, "rev-parse", "HEAD")
    archive = _release_zip(tmp_path / "gupkg-0.1.0-win-amd64.zip", version="0.1.0", commit=head)

    status = release.main(["first release", "--zip", str(archive), "--no-publish"], root=repository)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    table = (repository / "RELEASES.md").read_text(encoding="utf-8")
    assert status == 0
    assert f"| 0.1.0 | `{head}` | " in table and f"| {digest} | first release |" in table
    assert table.index("| 0.1.0 |") < table.index("| 0.0.9 |")
    assert _git(repository, "rev-parse", "v0.1.0^{commit}") == head
    assert _git(repository, "log", "-1", "--format=%s") == "Release 0.1.0"


def test_publishing_pushes_the_tag_and_uploads_the_zip_before_recording_the_row(repository: Path, tmp_path: Path) -> None:
    """With publishing, the tag reaches the remote and gh creates release v<version> with the ZIP as its asset."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git(repository, "remote", "add", "origin", str(remote))
    head = _git(repository, "rev-parse", "HEAD")
    archive = _release_zip(tmp_path / "gupkg-0.1.0-win-amd64.zip", version="0.1.0", commit=head)
    log = tmp_path / "gh.log"
    gh = tmp_path / "gh.cmd"
    # Stand-in gh: record every call; "release download" hands back the uploaded ZIP.
    gh.write_text(
        f'@echo off\necho %*>> "{log}"\nif "%2"=="download" copy /y "{archive}" "%~7\\%~5" >nul\n',
        encoding="ascii",
    )

    status = release.main(["notes here", "--zip", str(archive), "--gh", str(gh)], root=repository)

    assert status == 0
    assert subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "v0.1.0^{commit}"], capture_output=True, text=True).stdout.strip() == head
    calls = log.read_text(encoding="utf-8").splitlines()
    sidecar = archive.with_name(archive.name + ".sha256")
    assert calls[0].startswith("release create v0.1.0 ") and str(archive) in calls[0] and str(sidecar) in calls[0]
    assert sidecar.read_text(encoding="ascii") == f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    assert calls[1].startswith("release download v0.1.0 --pattern ")
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in (repository / "RELEASES.md").read_text(encoding="utf-8")


def test_a_published_asset_that_differs_from_the_local_zip_stops_the_release(repository: Path, tmp_path: Path) -> None:
    """If the downloaded asset has another SHA-256, the release stops and no row is recorded."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git(repository, "remote", "add", "origin", str(remote))
    head = _git(repository, "rev-parse", "HEAD")
    archive = _release_zip(tmp_path / "gupkg-0.1.0-win-amd64.zip", version="0.1.0", commit=head)
    other = tmp_path / "other.bin"
    other.write_bytes(b"not the release")
    gh = tmp_path / "gh.cmd"
    gh.write_text(f'@echo off\nif "%2"=="download" copy /y "{other}" "%~7\\%~5" >nul\n', encoding="ascii")

    with pytest.raises(SystemExit, match="the row was not recorded"):
        release.main(["notes", "--zip", str(archive), "--gh", str(gh)], root=repository)

    assert "| 0.1.0 |" not in (repository / "RELEASES.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "problem",
    ["dirty-tree", "version-listed", "zip-from-another-commit", "zip-for-another-version"],
)
def test_release_is_refused_before_anything_is_tagged_or_recorded(repository: Path, tmp_path: Path, problem: str) -> None:
    """Each refused situation leaves no tag, no new row and no extra commit."""
    head = _git(repository, "rev-parse", "HEAD")
    version, commit = "0.1.0", head
    if problem == "dirty-tree":
        (repository / "notes.txt").write_text("uncommitted", encoding="utf-8")
    elif problem == "version-listed":
        (repository / "RELEASES.md").write_text(TABLE.format("0" * 40) + f"| 0.1.0 | `{'1' * 40}` | 2026-01-02 | {'a' * 64} | x |\n", encoding="utf-8")
        _git(repository, "commit", "-q", "-am", "list it")
        head = commit = _git(repository, "rev-parse", "HEAD")
    elif problem == "zip-from-another-commit":
        commit = "f" * 40
    else:
        version = "0.2.0"
    archive = _release_zip(tmp_path / "release.zip", version=version, commit=commit)
    before = (repository / "RELEASES.md").read_text(encoding="utf-8")

    with pytest.raises(SystemExit):
        release.main(["notes", "--zip", str(archive), "--no-publish"], root=repository)

    assert _git(repository, "tag") == ""
    assert (repository / "RELEASES.md").read_text(encoding="utf-8") == before
    assert _git(repository, "rev-parse", "HEAD") == head
