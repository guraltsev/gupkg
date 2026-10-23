"""Cover how a registry is obtained: a trusted Git clone, or the repository's archive.

Behavior covered: Git is used only from a folder that non-administrators cannot
modify, and a user-writable ``git.exe`` earlier on PATH is skipped; a trusted Git
syncs by cloning, skips the clone when the remote revision is already cached, and
never publishes Git metadata; without a trusted Git a GitHub repository is
downloaded as its archive; and a Git-only source without Git is a clear failure.
Stood in by ``FakeSystem``: which folders are admin-only, the ``git`` child
process (a process double that answers ls-remote, clone and rev-parse), and the
archive download. Real: the PATH search for ``git.exe`` stubs, the cache layout
and validation. Out of scope: Git itself.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from gupkg import registry
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult

SOURCE = "https://github.com/example/gupkg-registry.git"
REVISION = "a" * 40
SEED = 'name = "tool"\nversion = "1.0.0"\nlocalVersion = 0\n'


def _git_double(revision: str = REVISION, calls: list[ProcessCall] | None = None):
    """Return a ``git`` process double that answers ls-remote, clone and rev-parse."""

    def run(call: ProcessCall) -> ProcessResult:
        command = list(call.argv)
        if calls is not None:
            calls.append(call)
        if "ls-remote" in command:
            return ProcessResult(0, f"{revision}\tHEAD\n")
        if "clone" in command:
            destination = Path(command[-1])
            (destination / ".git").mkdir(parents=True)
            (destination / "README.md").write_text("registry", encoding="utf-8")
            (destination / "tool" / "1.0.0").mkdir(parents=True)
            (destination / "tool" / "1.0.0" / "pkg.toml").write_text(SEED, encoding="utf-8")
            return ProcessResult(0)
        if "rev-parse" in command:
            return ProcessResult(0, revision + "\n")
        raise AssertionError(f"unexpected command {command}")

    return run


def _git_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a stub ``git.exe`` in a folder on PATH and return its path; PATH holds nothing else."""
    folder = tmp_path / "gitbin"
    folder.mkdir()
    git = folder / "git.exe"
    git.touch()
    monkeypatch.setenv("PATH", str(folder))
    return git


def test_git_in_a_user_writable_folder_is_skipped_for_a_trusted_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git.exe whose folder fails the admin-only check is never chosen, even when it comes first on PATH."""
    unsafe, safe = tmp_path / "unsafe", tmp_path / "safe"
    for folder in (unsafe, safe):
        folder.mkdir()
        (folder / "git.exe").touch()
    monkeypatch.setenv("PATH", f"{unsafe};{safe}")
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={unsafe})

    assert registry.trusted_git(system) == str(safe / "git.exe")


def test_no_trusted_git_means_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When every git.exe is in a user-writable folder, Git is not used at all."""
    git = _git_on_path(tmp_path, monkeypatch)
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={git.parent})

    assert registry.trusted_git(system) is None


def test_trusted_git_clones_publishes_packages_only_and_skips_an_unchanged_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone publishes the packages without Git metadata, and a second sync of an unchanged revision clones nothing."""
    _git_on_path(tmp_path, monkeypatch)
    calls: list[ProcessCall] = []
    system = FakeSystem(tmp_path / "machine", processes={"git": _git_double(calls=calls)})
    cache = tmp_path / "cache"

    first = registry.sync_registry(cache, system, source=SOURCE)
    clones_after_first = sum("clone" in call.argv for call in calls)
    second = registry.sync_registry(cache, system, source=SOURCE)

    assert first.ok and first.status == "synced", first.errors
    assert second.ok and second.status == "current"
    assert clones_after_first == 1 and sum("clone" in call.argv for call in calls) == 1
    state = registry.registry_status(cache, system)
    assert state.revision == REVISION and state.tree_path == cache / "gupkg-registry" / "trees" / REVISION / "pkgs"
    published = {path.name for path in state.tree_path.iterdir()}
    assert "tool" in published and ".git" not in published
    assert [item.selector for item in registry.search_registry(cache, system)] == ["tool"]


def test_clone_only_allows_https_and_ignores_user_git_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone is restricted to HTTPS without hooks, and GIT_* variables from the user are dropped."""
    _git_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    calls: list[ProcessCall] = []
    system = FakeSystem(tmp_path / "machine", processes={"git": _git_double(calls=calls)})

    assert registry.sync_registry(tmp_path / "cache", system, source=SOURCE).ok

    clone = next(call for call in calls if "clone" in call.argv)
    command = " ".join(clone.argv)
    assert "protocol.allow=never" in command and "protocol.https.allow=always" in command and "--depth 1" in command
    assert "core.hooksPath=" in command
    assert "GIT_SSH_COMMAND" not in clone.env and clone.env["GIT_TERMINAL_PROMPT"] == "0"


def test_without_trusted_git_a_github_repository_is_downloaded_as_its_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a trusted Git the repository's default-branch archive is fetched and published."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.comment = REVISION.encode()
        archive.writestr("gupkg-registry-main/README.md", "registry")
        archive.writestr("gupkg-registry-main/tool/1.0.0/pkg.toml", SEED)
    system = FakeSystem(
        tmp_path / "machine",
        urls={"https://github.com/example/gupkg-registry/archive/HEAD.zip": buffer.getvalue()},
    )

    result = registry.sync_registry(tmp_path / "cache", system, source=SOURCE)

    assert result.ok, result.errors
    assert system.fetch_log == ["https://github.com/example/gupkg-registry/archive/HEAD.zip"]
    assert registry.registry_status(tmp_path / "cache", system).revision == REVISION


def test_git_only_source_without_git_fails_and_keeps_the_previous_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: FakeSystem
) -> None:
    """A non-GitHub Git URL cannot be fetched without Git, and the failure is reported."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))

    result = registry.sync_registry(tmp_path / "cache", system, source="https://git.example/team/registry.git")

    assert not result.ok
    assert "github.com" in result.errors[0]
