"""Cover the Git provider and the Git unpack kind with a ``git`` process double.

``git`` runs only through ``System.run``; the double answers like Git (resolving a ref,
fetching one commit, checking it out) and records every command line and environment, so the
tests check what gupkg asks of Git, how it handles the answers, and the safety rules
(prompts off, ``--`` before untrusted values, a pinned commit that must match). Out of scope:
Git itself, and real clones (a manual smoke step).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, parse_manifest, render_pinned
from gupkg.services.populate import populate_git
from gupkg.services.prepare import PrepareError
from gupkg.services.track import TrackError, run_tracker
from gupkg.services.unpack import UnpackError, UnpackPlan, unpack_git
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.http_fixtures import environment

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER = "f" * 40
URL = "https://example.org/team/tool.git"
EPOCH = 1728720930  # 2024-10-12 08:15:30 UTC


class FakeGit:
    """A scripted ``git``: records calls and answers `ls-remote`, `fetch`, `log`, `rev-parse` and `checkout`."""

    def __init__(self, *, served: str = COMMIT, listed: str = COMMIT, files: dict[str, str] | None = None) -> None:
        """Configure the commit that `fetch` serves, the one `ls-remote` lists, and the working tree `checkout` creates."""
        self.served, self.listed = served, listed
        self.files = files if files is not None else {"src/main.py": "print()", "README": "r"}
        self.calls: list[ProcessCall] = []

    def __call__(self, call: ProcessCall) -> ProcessResult:
        """Answer one git command."""
        self.calls.append(call)
        words = call.argv[1:]
        if words[0] == "ls-remote":
            return ProcessResult(0, f"{self.listed}\t{words[-1]}\n") if self.listed else ProcessResult(2)
        if words[0] == "log":
            return ProcessResult(0, f"{EPOCH}\n")
        if words[0] == "rev-parse":
            return ProcessResult(0, f"{self.served}\n")
        if words[0] == "checkout":
            for name, text in self.files.items():
                path = call.cwd / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (call.cwd / ".git").mkdir()
            (call.cwd / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        return ProcessResult(0)

    def words(self) -> list[str]:
        """Return the first word after `git` of each recorded call."""
        return [call.argv[1] for call in self.calls]


def machine(tmp_path: Path, git: FakeGit) -> FakeSystem:
    """Return a FakeSystem whose only process is the scripted git."""
    return FakeSystem(tmp_path / "machine", processes={"git": git}, real_7zip=False)


def tracker(ref: str = "refs/heads/main") -> str:
    """Return the text of a git tracker."""
    return (
        f'format = 2\n[track]\nprovider = "git"\nrepo_url = "{URL}"\nref = "{ref}"\n'
        '[source]\ndownload_url = "https://example.org/team/tool.git"\nkind = "git"\n'
        '[source.git]\nref = "refs/heads/main"\ncommit = "${commit}"\n'
    )


def test_the_git_provider_versions_a_commit_by_its_date_and_short_id(tmp_path: Path) -> None:
    """The version is `YYYYMMDD-HHMMSS-<short sha>` in UTC, the full commit is a capture, and the pinned manifest names it."""
    git = FakeGit()
    system = machine(tmp_path, git)
    manifest = parse_manifest(tracker(), ManifestKind.TRACKER)

    result = run_tracker(manifest, environment(system, tmp_path))
    pinned = render_pinned(manifest, result)

    assert result.version == "20241012-081530-0123456"
    assert result.captures["commit"] == COMMIT and pinned.source.git.commit == COMMIT
    assert git.words() == ["ls-remote", "init", "fetch", "log"]


def test_git_runs_with_prompts_off_safe_transports_and_double_dash_before_untrusted_values(tmp_path: Path) -> None:
    """Every command has no terminal prompt and an allow-list of protocols, and the repository and ref follow `--`."""
    git = FakeGit()
    system = machine(tmp_path, git)

    run_tracker(parse_manifest(tracker(), ManifestKind.TRACKER), environment(system, tmp_path))

    for call in git.calls:
        assert call.env["GIT_TERMINAL_PROMPT"] == "0"
        assert call.env["GIT_ALLOW_PROTOCOL"] == "https:http:ssh:git"
    listing = git.calls[0].argv
    assert listing[listing.index("--") + 1 :] == (URL, "refs/heads/main")


def test_a_ref_that_resolves_to_nothing_is_a_track_error(tmp_path: Path) -> None:
    """When `git ls-remote` finds no such ref the provider fails with a message naming the ref."""
    system = machine(tmp_path, FakeGit(listed=""))

    with pytest.raises(TrackError, match="ls-remote failed|no commit"):
        run_tracker(parse_manifest(tracker("refs/heads/nope"), ManifestKind.TRACKER), environment(system, tmp_path))


def test_a_repository_address_that_looks_like_an_option_fails_validation() -> None:
    """`-oProxyCommand=...` can never reach Git as a repository: the manifest is invalid."""
    with pytest.raises(Exception, match="https://, ssh:// or git://"):
        parse_manifest('format = 2\n[track]\nprovider = "git"\nrepo_url = "--upload-pack=evil"\n[source]\ndownload_url = "x"\n', ManifestKind.TRACKER)


def pinned_git_manifest(commit: str):
    """Return a pinned git version manifest with one prepare step."""
    return parse_manifest(
        f'format = 2\n[source]\ndownload_url = "{URL}"\nkind = "git"\n[source.git]\ncommit = "{commit}"\n'
        '[[prepare]]\nremove = ["README"]\n', ManifestKind.VERSION)


def test_a_pinned_commit_is_checked_out_without_history_and_prepared(tmp_path: Path) -> None:
    """The working tree of exactly the pinned commit lands in `App` without `.git`, and the prepare steps ran."""
    git = FakeGit()
    system = machine(tmp_path, git)
    stage = tmp_path / "stage"
    stage.mkdir()

    report = populate_git(system, pinned_git_manifest(COMMIT), stage / "App")

    assert report.commit == COMMIT and report.kind == "git"
    assert sorted(path.relative_to(stage / "App").as_posix() for path in (stage / "App").rglob("*") if path.is_file()) == ["src/main.py"]
    assert "ls-remote" not in git.words() and not (stage / "App" / ".git").exists()


def test_a_server_that_serves_another_commit_than_the_pinned_one_fails_and_leaves_nothing(tmp_path: Path) -> None:
    """A pinned commit that the repository does not serve is refused: a moved branch cannot change what a version installs."""
    system = machine(tmp_path, FakeGit(served=OTHER))
    stage = tmp_path / "stage"
    stage.mkdir()

    with pytest.raises(UnpackError, match="not the pinned"):
        unpack_git(system, URL, None, COMMIT, stage / "App", UnpackPlan())

    assert list(stage.iterdir()) == []


def test_an_unpinned_ref_resolves_to_one_commit_that_is_then_fetched(tmp_path: Path) -> None:
    """With only a ref, the commit `ls-remote` reports is the one fetched and returned."""
    git = FakeGit()
    system = machine(tmp_path, git)
    stage = tmp_path / "stage"
    stage.mkdir()

    report = unpack_git(system, URL, "refs/heads/main", None, stage / "App", UnpackPlan())

    assert report.commit == COMMIT and git.words()[0] == "ls-remote"


def test_a_failing_prepare_step_after_checkout_removes_the_app(tmp_path: Path) -> None:
    """If a `[[prepare]]` step fails after the checkout, no `App` is left."""
    manifest = parse_manifest(
        f'format = 2\n[source]\ndownload_url = "{URL}"\nkind = "git"\n[source.git]\ncommit = "{COMMIT}"\n'
        '[[prepare]]\nrename = { from = "nothing", to = "x" }\n', ManifestKind.VERSION)
    system = machine(tmp_path, FakeGit())
    stage = tmp_path / "stage"
    stage.mkdir()

    with pytest.raises(PrepareError):
        populate_git(system, manifest, stage / "App")

    assert list(stage.iterdir()) == []


def test_an_address_starting_with_a_dash_is_refused_by_the_checkout(tmp_path: Path) -> None:
    """Even called directly, the checkout refuses a repository argument that Git would read as an option."""
    stage = tmp_path / "stage"
    stage.mkdir()

    with pytest.raises(UnpackError, match="cannot start with"):
        unpack_git(machine(tmp_path, FakeGit()), "--evil", None, COMMIT, stage / "App", UnpackPlan())
