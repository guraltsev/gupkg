"""Cover how a registry is obtained: a trusted Git clone, or the repository's archive.

Behavior covered: Git is used only from a folder that non-administrators cannot
modify, and a user-writable ``git.exe`` earlier on PATH is skipped; a trusted Git
syncs by cloning, skips the clone when the remote revision is already cached, and
never publishes Git metadata; without a trusted Git a GitHub repository is
downloaded as its archive; and a Git-only source without Git is a clear failure.
Mocked boundaries: the access-control check, the ``git`` subprocess, and the
archive download. Real: the cache layout and validation. Out of scope: Git itself.
"""

from __future__ import annotations

import io
import types
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from gupkg import registry
from gupkg.elevation import UntrustedLocationError

SOURCE = "https://github.com/example/gupkg-registry.git"
REVISION = "a" * 40
SEED = 'name = "tool"\nversion = "1.0.0"\nlocalVersion = 0\n'


def _fake_git(revision: str = REVISION, calls: list[list[str]] | None = None):
    """Return a subprocess.run stand-in that answers ls-remote, clone and rev-parse."""

    def run(command, **kwargs):
        if calls is not None:
            calls.append([str(part) for part in command])
        if "ls-remote" in command:
            return types.SimpleNamespace(returncode=0, stdout=f"{revision}\tHEAD\n", stderr="")
        if "clone" in command:
            destination = Path(command[-1])
            (destination / ".git").mkdir(parents=True)
            (destination / "README.md").write_text("registry", encoding="utf-8")
            (destination / "tool" / "1.0.0").mkdir(parents=True)
            (destination / "tool" / "1.0.0" / "pkg.toml").write_text(SEED, encoding="utf-8")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "rev-parse" in command:
            return types.SimpleNamespace(returncode=0, stdout=revision + "\n", stderr="")
        raise AssertionError(f"unexpected command {command}")

    return run


def test_git_in_a_user_writable_folder_is_skipped_for_a_trusted_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A git.exe whose folder fails the admin-only check is never chosen, even when it comes first on PATH."""
    unsafe, safe = tmp_path / "unsafe", tmp_path / "safe"
    for folder in (unsafe, safe):
        folder.mkdir()
        (folder / "git.exe").touch()
    monkeypatch.setenv("PATH", f"{unsafe};{safe}")

    def check(directory: Path) -> None:
        if directory.resolve() == unsafe.resolve():
            raise UntrustedLocationError("writable")

    with mock.patch.object(registry, "require_trusted_directory", check):
        assert registry.trusted_git() == str(safe / "git.exe")


def test_no_trusted_git_means_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When every git.exe is in a user-writable folder, Git is not used at all."""
    (tmp_path / "git.exe").touch()
    monkeypatch.setenv("PATH", str(tmp_path))

    with mock.patch.object(registry, "require_trusted_directory", side_effect=UntrustedLocationError("writable")):
        assert registry.trusted_git() is None


def test_trusted_git_clones_publishes_packages_only_and_skips_an_unchanged_revision(tmp_path: Path) -> None:
    """A clone publishes the packages without Git metadata, and a second sync of an unchanged revision clones nothing."""
    cache = tmp_path / "cache"
    calls: list[list[str]] = []

    with mock.patch.object(registry, "trusted_git", return_value="git"), mock.patch("subprocess.run", _fake_git(calls=calls)):
        first = registry.sync_registry(cache, source=SOURCE)
        clones_after_first = sum("clone" in call for call in calls)
        second = registry.sync_registry(cache, source=SOURCE)

    assert first.ok and first.status == "synced", first.errors
    assert second.ok and second.status == "current"
    assert clones_after_first == 1 and sum("clone" in call for call in calls) == 1
    state = registry.registry_status(cache)
    assert state.revision == REVISION and state.tree_path == cache / "gupkg-registry" / "trees" / REVISION / "pkgs"
    published = {path.name for path in state.tree_path.iterdir()}
    assert "tool" in published and ".git" not in published
    assert [item.selector for item in registry.search_registry(cache)] == ["tool"]


def test_clone_only_allows_https_and_ignores_user_git_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The clone is restricted to HTTPS without hooks, and GIT_* variables from the user are dropped."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    seen: dict[str, object] = {}
    inner = _fake_git()

    def run(command, **kwargs):
        if "clone" in command:
            seen["command"], seen["env"] = [str(part) for part in command], kwargs["env"]
        return inner(command, **kwargs)

    with mock.patch.object(registry, "trusted_git", return_value="git"), mock.patch("subprocess.run", run):
        assert registry.sync_registry(tmp_path / "cache", source=SOURCE).ok

    command = " ".join(seen["command"])
    assert "protocol.allow=never" in command and "protocol.https.allow=always" in command and "--depth 1" in command
    assert "core.hooksPath=" in command
    assert "GIT_SSH_COMMAND" not in seen["env"] and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_without_trusted_git_a_github_repository_is_downloaded_as_its_archive(tmp_path: Path) -> None:
    """Without a trusted Git the repository's default-branch archive is fetched and published."""
    requested: list[str] = []

    def download(source: str, destination: Path) -> None:
        requested.append(source)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.comment = REVISION.encode()
            archive.writestr("gupkg-registry-main/README.md", "registry")
            archive.writestr("gupkg-registry-main/tool/1.0.0/pkg.toml", SEED)
        destination.write_bytes(buffer.getvalue())

    with mock.patch.object(registry, "trusted_git", return_value=None), mock.patch.object(registry, "_download", download):
        result = registry.sync_registry(tmp_path / "cache", source=SOURCE)

    assert result.ok, result.errors
    assert requested == ["https://github.com/example/gupkg-registry/archive/HEAD.zip"]
    assert registry.registry_status(tmp_path / "cache").revision == REVISION


def test_git_only_source_without_git_fails_and_keeps_the_previous_tree(tmp_path: Path) -> None:
    """A non-GitHub Git URL cannot be fetched without Git, and the failure is reported."""
    with mock.patch.object(registry, "trusted_git", return_value=None):
        result = registry.sync_registry(tmp_path / "cache", source="https://git.example/team/registry.git")

    assert not result.ok
    assert "github.com" in result.errors[0]
