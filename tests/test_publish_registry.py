"""Cover publishing the registry by moving and pushing the ``registry`` tag.

Behavior covered: a valid pushed revision gets the tag locally and on the remote;
a failing check (launcher missing, unpushed revision) changes nothing; ``--force``
publishes anyway; and ``--target`` publishes an older revision instead of HEAD.
Real: the tool, Git, a temporary repository and a local bare remote. Out of
scope: the contents of real packages.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "publish_registry.py"
LAUNCHER = b"@echo off\r\nrem launcher\r\n"


def _git(repo: Path, *arguments: str) -> str:
    """Run Git in *repo* with a fixed identity and return its output."""
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, *, launcher: bool = True) -> str:
    """Commit a one-package registry whose seed does or does not carry the launcher."""
    seed = repo / "pkgs" / "tool" / "pkg.bootstrap"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "pkg.toml").write_text(f'name = "tool"\nversion = "bootstrap"\nlocalVersion = 0\n# {message}\n', encoding="utf-8")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "gupkg.cmd").write_bytes(LAUNCHER)
    if launcher:
        (seed / "gupkg.cmd").write_bytes(LAUNCHER)
    else:
        (seed / "gupkg.cmd").unlink(missing_ok=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with a local bare remote named origin, one valid commit already pushed."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "remote", "add", "origin", str(remote))
    _commit(work, "first")
    _git(work, "push", "origin", "main")
    return work


def _publish(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the publishing tool against *repo*."""
    return subprocess.run(
        [sys.executable, str(TOOL), "--repo", str(repo), *arguments], capture_output=True, text=True
    )


def _remote_tag(repo: Path) -> str:
    """Return the commit the remote's registry tag points at, or an empty string."""
    output = _git(repo, "ls-remote", "origin", "refs/tags/registry")
    return output.split()[0] if output else ""


def test_valid_pushed_revision_moves_and_pushes_the_tag(repo: Path) -> None:
    """With every check passing, the tag lands on HEAD locally and on the remote."""
    head = _git(repo, "rev-parse", "HEAD")

    result = _publish(repo)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "registry^{commit}") == head
    assert _remote_tag(repo) == head


def test_failed_check_changes_nothing(repo: Path) -> None:
    """A package without the launcher stops the publication before any tag moves."""
    _commit(repo, "drop launcher", launcher=False)
    _git(repo, "push", "origin", "main")

    result = _publish(repo)

    assert result.returncode == 1
    assert "gupkg.cmd" in result.stderr
    assert _remote_tag(repo) == ""
    assert _git(repo, "tag", "-l", "registry") == ""


def test_unpushed_revision_is_refused(repo: Path) -> None:
    """A commit that is not on any remote branch is not published."""
    _commit(repo, "local only")

    result = _publish(repo)

    assert result.returncode == 1
    assert "not on any remote branch" in result.stderr
    assert _remote_tag(repo) == ""


def test_force_publishes_even_when_a_check_fails(repo: Path) -> None:
    """--force moves and pushes the tag despite a failing check, and still reports the problem."""
    broken = _commit(repo, "drop launcher", launcher=False)

    result = _publish(repo, "--force")

    assert result.returncode == 0, result.stderr
    assert "warning:" in result.stderr
    assert _remote_tag(repo) == broken


def test_target_publishes_the_given_revision_instead_of_head(repo: Path) -> None:
    """--target selects an older, valid revision and replaces an existing tag with it."""
    first = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "second")
    _git(repo, "push", "origin", "main")
    assert _publish(repo).returncode == 0

    result = _publish(repo, "--target", first)

    assert result.returncode == 0, result.stderr
    assert _remote_tag(repo) == first


def test_unknown_target_is_an_error(repo: Path) -> None:
    """A revision that does not exist is rejected even with --force."""
    result = _publish(repo, "--target", "no-such-revision", "--force")

    assert result.returncode == 2
    assert _remote_tag(repo) == ""
