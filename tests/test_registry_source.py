"""Cover how a registry is obtained (a trusted Git clone, or the repository's archive) and which cache locations an elevated run accepts.

Behavior covered: Git is used only from a folder that non-administrators cannot modify, and a user-writable ``git.exe`` earlier on
PATH is skipped; a trusted Git syncs by cloning, skips the clone when the remote revision is already cached, never publishes Git
metadata and runs without the user's ``GIT_*`` variables; without a trusted Git a GitHub repository is downloaded as its archive;
a Git-only source without Git is a clear failure that keeps the previous tree; an elevated run refuses to read or sync a registry
cache in a folder non-administrators can modify (also through ``gupkg registry status``), an ordinary run is unaffected.

Real: the PATH search for ``git.exe`` stubs, the cache layout and validation, the command line. Stood in by ``FakeSystem``: which
folders are admin-only, the ``git`` child process (a process double that answers ls-remote, clone and rev-parse) and the archive
download. Out of scope: Git itself, and the Windows access-control reader (``tests/test_system_contract.py``).
"""

from __future__ import annotations

import io
import warnings
import zipfile
from pathlib import Path

import pytest

from gupkg.catalog.registry import cache_state, load_registry, registry_cache_dir, sync_registry
from gupkg.core import ConfigValidationError
from gupkg.services import registrysource
from gupkg.services.registrysource import trusted_git
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from gupkg.tomlio import dumps
from tests.catalog_support import version_text
from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401

SOURCE = "https://github.com/example/gupkg-registry.git"
REVISION = "a" * 40
REGISTRY_FILE = dumps({"format": 2, "name": "main"})


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
            (destination / "_registry.toml").write_text(REGISTRY_FILE, encoding="utf-8")
            (destination / "tool" / "1.0.0").mkdir(parents=True)
            (destination / "tool" / "1.0.0" / "pkg.toml").write_text(version_text("1.0.0"), encoding="utf-8")
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


def test_git_in_a_user_writable_folder_is_skipped_for_a_trusted_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A git.exe whose folder fails the admin-only check is never chosen, even when it comes first on PATH."""
    unsafe, safe = tmp_path / "unsafe", tmp_path / "safe"
    for folder in (unsafe, safe):
        folder.mkdir()
        (folder / "git.exe").touch()
    monkeypatch.setenv("PATH", f"{unsafe};{safe}")
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={unsafe})

    assert trusted_git(system) == str(safe / "git.exe")


def test_no_trusted_git_means_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When every git.exe is in a user-writable folder, Git is not used at all."""
    git = _git_on_path(tmp_path, monkeypatch)
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={git.parent})

    assert trusted_git(system) is None


def test_trusted_git_clones_publishes_packages_only_and_skips_an_unchanged_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clone publishes the packages without Git metadata, and a second sync of an unchanged revision clones nothing."""
    _git_on_path(tmp_path, monkeypatch)
    calls: list[ProcessCall] = []
    system = FakeSystem(tmp_path / "machine", processes={"git": _git_double(calls=calls)})
    cache = registry_cache_dir(tmp_path / "root", "main")

    first = sync_registry(cache, system, name="main", source=SOURCE)
    clones_after_first = sum("clone" in call.argv for call in calls)
    second = sync_registry(cache, system, name="main", source=SOURCE)

    assert first.ok and first.status == "synced", first.error
    assert second.ok and second.status == "current"
    assert clones_after_first == 1 and sum("clone" in call.argv for call in calls) == 1
    state = cache_state(cache, system)
    assert state.revision == REVISION and state.tree_dir == cache / "trees" / REVISION
    published = {path.name for path in (state.tree_dir / "pkgs").iterdir()}
    assert "tool" in published and ".git" not in published and "_registry.toml" not in published
    assert sorted(load_registry("main", cache, system).entries) == ["tool"]


def test_clone_only_allows_https_and_ignores_user_git_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The clone is restricted to HTTPS without hooks, and GIT_* variables from the user are dropped."""
    _git_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    calls: list[ProcessCall] = []
    system = FakeSystem(tmp_path / "machine", processes={"git": _git_double(calls=calls)})

    assert sync_registry(registry_cache_dir(tmp_path / "root", "main"), system, name="main", source=SOURCE).ok

    clone = next(call for call in calls if "clone" in call.argv)
    command = " ".join(clone.argv)
    assert "protocol.allow=never" in command and "protocol.https.allow=always" in command and "--depth 1" in command
    assert "core.hooksPath=" in command
    assert "GIT_SSH_COMMAND" not in clone.env and clone.env["GIT_TERMINAL_PROMPT"] == "0"


def test_without_trusted_git_a_github_repository_is_downloaded_as_its_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a trusted Git the repository's default-branch archive is fetched and published."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.comment = REVISION.encode()
        archive.writestr("gupkg-registry-main/README.md", "registry")
        archive.writestr("gupkg-registry-main/_registry.toml", REGISTRY_FILE)
        archive.writestr("gupkg-registry-main/tool/1.0.0/pkg.toml", version_text("1.0.0"))
    system = FakeSystem(tmp_path / "machine", urls={"https://github.com/example/gupkg-registry/archive/HEAD.zip": buffer.getvalue()})
    cache = registry_cache_dir(tmp_path / "root", "main")

    result = sync_registry(cache, system, name="main", source=SOURCE)

    assert result.ok, result.error
    assert system.fetch_log == ["https://github.com/example/gupkg-registry/archive/HEAD.zip"]
    assert cache_state(cache, system).revision == REVISION


def test_git_only_source_without_git_fails_and_keeps_the_previous_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-GitHub Git URL cannot be fetched without Git, and the failure is reported."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    system = FakeSystem(tmp_path / "machine")

    result = sync_registry(registry_cache_dir(tmp_path / "root", "main"), system, name="main", source="https://git.example/team/registry.git")

    assert not result.ok and "github.com" in (result.error or "")


def test_elevated_run_refuses_a_user_writable_registry_cache(tmp_path: Path) -> None:
    """Under elevation, reading or syncing a cache in a user-writable folder fails with a message that names the cache."""
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={tmp_path})
    cache = tmp_path / "cache"

    with pytest.raises(ConfigValidationError, match="Refusing the registry cache"):
        cache_state(cache, system)
    result = sync_registry(cache, system, name="main", source="https://example.org/main.zip")

    assert not result.ok and "Refusing the registry cache" in (result.error or "")
    assert load_registry("main", cache, system).problems


def test_elevated_run_accepts_a_registry_cache_in_an_admin_only_folder(tmp_path: Path) -> None:
    """Under elevation, a cache whose folder only administrators can modify is read normally."""
    system = FakeSystem(tmp_path / "machine", elevated=True)

    assert cache_state(tmp_path / "cache", system).revision is None


def test_ordinary_run_reads_any_registry_cache_location(tmp_path: Path) -> None:
    """Without elevation the cache location is unrestricted and an empty cache is simply empty."""
    system = FakeSystem(tmp_path / "machine", elevated=False, untrusted_dirs={tmp_path})

    assert cache_state(tmp_path / "cache", system).revision is None


def test_the_registry_status_command_reports_the_refusal_as_an_error(tmp_path: Path) -> None:
    """The command line turns the refusal into a normal error (exit 2 with a structured error), not a crash."""
    world = CliWorld(tmp_path, elevated=True)
    cache_folder = world.system_root / ".gupkg" / "registry"
    cache_folder.mkdir(parents=True)
    world.fake.untrusted_dirs.add(cache_folder)

    result = world.cli("registry", "status", "--json")

    document = result.document()
    assert result.code == 2 and document["ok"] is False
    assert document["errors"][0]["code"] == "invalid-configuration" and "Refusing the registry cache" in document["errors"][0]["message"]


# ---------------------------------------------------------------------------
# The archive route: what an archive may contain, and which sources are accepted
# ---------------------------------------------------------------------------

ARCHIVE_URL = "https://registry.example/registry.zip"


def _good_entries() -> list[tuple[str | zipfile.ZipInfo, bytes | str]]:
    """Return the entries of a valid GitHub-style registry archive."""
    return [
        ("wrap/_registry.toml", REGISTRY_FILE),
        ("wrap/tool/1.0.0/pkg.toml", version_text("1.0.0")),
    ]


def _archive(entries, comment: str = REVISION) -> bytes:
    """Return the bytes of a ZIP archive holding *entries* (names or prepared ``ZipInfo`` objects) with the commit as its comment."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # a duplicate name is the point of some archives
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.comment = comment.encode()
            for name, data in entries:
                archive.writestr(name, data)
    return buffer.getvalue()


def _symlink(name: str) -> zipfile.ZipInfo:
    """Return the header of a symbolic-link entry called *name*."""
    info = zipfile.ZipInfo(name)
    info.external_attr = (0o120000 << 16) | 0x20
    return info


HOSTILE_ARCHIVES = {
    "dot-dot": [("wrap/../../escape.txt", "x")],
    "absolute": [("/escape.txt", "x")],
    "drive-letter": [("C:/escape.txt", "x")],
    "drive-relative": [("C:escape.txt", "x")],
    "backslash-traversal": [("wrap\\..\\..\\escape.txt", "x")],
    "symlink": [(_symlink("wrap/link"), "C:\\Windows")],
    "duplicate-name": [("wrap/tool/1.0.0/pkg.toml", "a"), ("wrap/tool/1.0.0/pkg.toml", "b")],
    "duplicate-name-other-case": [("wrap/tool/1.0.0/pkg.toml", "a"), ("wrap/Tool/1.0.0/PKG.toml", "b")],
}


@pytest.mark.parametrize("entries", [pytest.param(value, id=key) for key, value in HOSTILE_ARCHIVES.items()])
def test_a_hostile_archive_is_refused_writes_nothing_outside_and_keeps_the_previous_tree(tmp_path: Path, entries: list) -> None:
    """Escaping, absolute, drive, link and repeated names fail the sync; no file appears outside the cache and the old tree stays."""
    system = FakeSystem(tmp_path / "machine", urls={"https://registry.example/good.zip": _archive(_good_entries(), "b" * 40)})
    cache = tmp_path / "cache"
    assert sync_registry(cache, system, name="main", source="https://registry.example/good.zip").ok
    system.add_url(ARCHIVE_URL, _archive([*_good_entries(), *entries]))

    result = sync_registry(cache, system, name="main", source=ARCHIVE_URL)

    assert not result.ok and "previous validated tree remains active" in (result.error or "")
    assert not list(tmp_path.rglob("escape.txt"))
    assert cache_state(cache, system).revision == "b" * 40


def test_a_hostile_archive_on_a_first_sync_publishes_no_tree(tmp_path: Path) -> None:
    """With nothing cached yet, a refused archive leaves no active tree behind."""
    system = FakeSystem(tmp_path / "machine", urls={ARCHIVE_URL: _archive([("wrap/../../escape.txt", "x")])})
    cache = tmp_path / "cache"

    result = sync_registry(cache, system, name="main", source=ARCHIVE_URL)

    assert not result.ok and cache_state(cache, system).tree_dir is None and not list(tmp_path.rglob("escape.txt"))


def test_an_archive_with_too_much_content_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The byte bound counts what is written: a larger archive is a failed sync, not a full disk."""
    monkeypatch.setattr(registrysource, "MAX_ARCHIVE_BYTES", 100)
    system = FakeSystem(tmp_path / "one", urls={ARCHIVE_URL: _archive([*_good_entries(), ("wrap/blob.bin", "x" * 5000)])})

    result = sync_registry(tmp_path / "cache", system, name="main", source=ARCHIVE_URL)

    assert not result.ok and "bytes of content" in (result.error or "")


def test_an_archive_with_too_many_entries_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry bound is checked before anything is written."""
    monkeypatch.setattr(registrysource, "MAX_ARCHIVE_ENTRIES", 2)
    system = FakeSystem(tmp_path / "two", urls={ARCHIVE_URL: _archive([("wrap/a", "1"), ("wrap/b", "2"), ("wrap/c", "3")])})

    result = sync_registry(tmp_path / "cache", system, name="main", source=ARCHIVE_URL)

    assert not result.ok and "entries" in (result.error or "")


def test_a_valid_archive_still_extracts(tmp_path: Path) -> None:
    """The checks refuse hostile names only: the plain archive of the other tests publishes its package."""
    system = FakeSystem(tmp_path / "machine", urls={ARCHIVE_URL: _archive(_good_entries())})

    result = sync_registry(tmp_path / "cache", system, name="main", source=ARCHIVE_URL)

    assert result.ok, result.error


@pytest.mark.parametrize("source", ["ftp://registry.example/registry.zip", "git@github.com:example/registry.git", "C:\\registries\\main.zip", "registry.zip"])
def test_a_registry_source_that_is_not_an_http_or_file_url_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """A source that is neither an http(s) nor a file URL (and no repository) fails the sync with a message, and nothing is fetched."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    system = FakeSystem(tmp_path / "machine")

    result = sync_registry(tmp_path / "cache", system, name="main", source=source)

    assert not result.ok and system.fetch_log == []
