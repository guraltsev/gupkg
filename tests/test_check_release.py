"""Cover the check of a published release against its RELEASES.md row.

Behavior covered: a correct release passes every check; a ZIP with other bytes than the row, a
``.sha256`` asset naming another digest, a tampered tree file, an unlisted extra file, a missing
tag and a missing row are each reported as FAIL with a failing exit status.
Real: the tool, a temporary Git repository with a bare remote, and real ZIP files.
Mocked: the download (a function that serves the ZIP and its ``.sha256`` from memory).
Out of scope: the real GitHub download, which scripts\\check-release.cmd performs.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_release  # noqa: E402

VERSION = "0.1.0"
NAME = f"gupkg-{VERSION}-win-amd64.zip"
BASE = "https://example.invalid/releases/download/v0.1.0"
TABLE = "| Version | Commit | Date | SHA-256 | Notes |\n|---|---|---|---|---|\n{row}"


def _git(repo: Path, *arguments: str) -> str:
    """Run Git in *repo* and return its stripped output."""
    return subprocess.run(["git", *arguments], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _zip_bytes(path: Path, commit: str, *, tamper: bool = False, extra: bool = False) -> bytes:
    """Write a release ZIP with RELEASE.txt, a payload file and a MANIFEST.sha256 of the tree; return its bytes."""
    payload = b"payload"
    tree = {"gupkg/RELEASE.txt": f"gupkg {VERSION}\ncommit {commit}\n".encode(), "gupkg/tool.py": payload}
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(tree.items()))

    def entry(name: str) -> zipfile.ZipInfo:
        """A ZIP entry with a fixed time, so equal content gives equal bytes."""
        return zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))

    with zipfile.ZipFile(path, "w") as archive:
        for name, data in tree.items():
            archive.writestr(entry(f"gupkg/{VERSION}/{name}"), b"changed" if tamper and name.endswith("tool.py") else data)
        archive.writestr(entry(f"gupkg/{VERSION}/gupkg/MANIFEST.sha256"), manifest)
        if extra:
            archive.writestr(entry(f"gupkg/{VERSION}/gupkg/unlisted.py"), b"x")
    return path.read_bytes()


def _setup(tmp_path: Path, *, tag: bool = True, row: bool = True, **zip_options: bool) -> tuple[Path, str, bytes]:
    """Create a repository with a pushed commit, a tag, a RELEASES.md row and the ZIP bytes."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "RELEASES.md").write_text(TABLE.format(row=""), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "start")
    commit = _git(repo, "rev-parse", "HEAD")
    data = _zip_bytes(tmp_path / NAME, commit, **zip_options)
    if row:
        line = f"| {VERSION} | `{commit}` | 2026-01-01 | {hashlib.sha256(_zip_bytes(tmp_path / 'good.zip', commit)).hexdigest()} | n |\n"
        (repo / "RELEASES.md").write_text(TABLE.format(row=line), encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "row")
    if tag:
        _git(repo, "tag", f"v{VERSION}", commit)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main", "--tags")
    return repo, commit, data


def _fetch(assets: dict[str, bytes]):
    """Return a download function that serves *assets* by file name and fails for anything else."""

    def fetch(url: str) -> bytes:
        name = url.rsplit("/", 1)[-1]
        if name not in assets:
            raise OSError(f"HTTP Error 404: {name}")
        return assets[name]

    return fetch


def _run(repo: Path, assets: dict[str, bytes]) -> tuple[bool, str]:
    """Run the checks and return the verdict and the report text."""
    passed, lines = check_release.check(repo, VERSION, BASE, "origin", _fetch(assets))
    return passed, "\n".join(lines)


def test_a_correct_release_passes_every_check(tmp_path: Path) -> None:
    """The row, tags, pushed branch, both digests, RELEASE.txt and the manifest all agree."""
    repo, _, data = _setup(tmp_path)
    digest = hashlib.sha256(data).hexdigest()

    passed, report = _run(repo, {NAME: data, f"{NAME}.sha256": f"{digest}  {NAME}\n".encode()})

    assert passed, report
    assert "FAIL" not in report and report.count("PASS") == 9


def test_a_zip_with_other_bytes_than_the_row_fails(tmp_path: Path) -> None:
    """If the downloaded ZIP differs from the digest in RELEASES.md, the check fails and says which."""
    repo, commit, data = _setup(tmp_path)

    passed, report = _run(repo, {NAME: data + b"x", f"{NAME}.sha256": b"0" * 64})

    assert not passed
    assert "FAIL  the downloaded ZIP has the SHA-256 of the RELEASES.md row" in report


def test_a_sha256_asset_naming_another_digest_fails(tmp_path: Path) -> None:
    """The ``.sha256`` asset must carry the digest of the row."""
    repo, _, data = _setup(tmp_path)

    passed, report = _run(repo, {NAME: data, f"{NAME}.sha256": f"{'0' * 64}  {NAME}\n".encode()})

    assert not passed
    assert f"FAIL  {NAME}.sha256 names the same SHA-256" in report


@pytest.mark.parametrize("option", ["tamper", "extra"])
def test_a_tree_that_differs_from_its_manifest_fails(tmp_path: Path, option: str) -> None:
    """A changed file or an unlisted file in the bundled tree is reported."""
    repo, commit, _ = _setup(tmp_path)
    data = _zip_bytes(tmp_path / "bad.zip", commit, **{option: True})
    (repo / "RELEASES.md").write_text(
        TABLE.format(row=f"| {VERSION} | `{commit}` | 2026-01-01 | {hashlib.sha256(data).hexdigest()} | n |\n"), encoding="utf-8"
    )

    passed, report = _run(repo, {NAME: data, f"{NAME}.sha256": f"{hashlib.sha256(data).hexdigest()}  {NAME}\n".encode()})

    assert not passed
    assert "FAIL" in report and ("exist with their SHA-256" in report or "no file the manifest" in report)


def test_a_missing_remote_tag_fails(tmp_path: Path) -> None:
    """A release whose tag was never pushed is reported."""
    repo, _, data = _setup(tmp_path, tag=False)
    digest = hashlib.sha256(data).hexdigest()

    passed, report = _run(repo, {NAME: data, f"{NAME}.sha256": f"{digest}  {NAME}\n".encode()})

    assert not passed
    assert f"FAIL  local tag v{VERSION}" in report and f"FAIL  origin has the tag v{VERSION}" in report


def test_a_missing_row_stops_the_check(tmp_path: Path) -> None:
    """Without a RELEASES.md row there is nothing to compare with, so the check fails at once."""
    repo, _, data = _setup(tmp_path, row=False)

    passed, report = _run(repo, {NAME: data})

    assert not passed
    assert report.splitlines() == [f"FAIL  RELEASES.md has a row for {VERSION} with a commit and a SHA-256"]


def test_a_missing_download_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    """A 404 for the ZIP is reported as a FAIL line."""
    repo, _, _ = _setup(tmp_path)

    passed, report = _run(repo, {})

    assert not passed
    assert "FAIL  download" in report and "404" in report
