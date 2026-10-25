"""Cover the registry tool: new version directories with hashes and fingerprints, and what it must never do.

Behavior covered: from a tracker and recorded vendor responses, ``update`` writes exactly the new
``<name>/<version>/`` directory (``pkg.toml`` with its ``sha256``, the tracker's support files without
update-check code, the launchers) and records its fingerprint in ``_registry.toml``, and that
fingerprint matches the files; a hash the vendor publishes is verified (a wrong artifact is fatal
and writes nothing) while a missing one is computed over ``https://`` only and marked
``sha256_from = "registry"`` and for review; unpack and prepare run in the sandbox folder, which
holds no staged ``App`` afterwards; every older version is kept byte for byte and a version that
already exists is left alone; a failing package never blocks the others; ``pin`` gives a legacy
version its first hash and refuses to replace one; the tool starts no ``git`` process unless
``--auto-push`` is given, then commits only what it wrote and pushes once; and a copy of a written
version directory verifies against the published fingerprint and is refused after one file changes.
Real: the services (providers, client, checksum policy, unpack, prepare, fingerprint), real files,
real 7-Zip. Mocked: ``FakeSystem`` serves the vendor responses and plays ``git``. Out of scope: the
live vendors (a manual step) and the install engine (issue 028).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import time
from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, manifest_to_toml, parse_manifest
from gupkg.pkgcheck import check_directory
from gupkg.report.events import Warning, use_reporter
from gupkg.services.fingerprint import fingerprint_directory, verify_directory
from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from gupkg.launchers import launcher_text, write_launchers
from gupkg.engine.locks import Holder, render_lock
from gupkg_registry.cli import main
from gupkg_registry.layout import RegistryFileError, read_registry_file, write_registry_file
from gupkg_registry.publish import commit_message
from gupkg_registry.update import Outcome, pin_version, update_registry
from gupkg_registry.validate import validate_registry
from tests.archive_support import make_zip
from tests.http_fixtures import FIXTURES, HTTP_CASES, load_case

SNAPSHOT = FIXTURES / "registry_100"
HWINFO_ZIP = {"HWiNFO64.EXE": "exe", "HWiNFO64.INI": "ini"}
VOLTA_ZIP = {"volta.exe": "v", "volta-shim.exe": "s", "volta-migrate.exe": "m"}


class Collect:
    """A reporter that keeps every event."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def handle(self, event: object) -> None:
        """Keep one event."""
        self.events.append(event)


class Git:
    """A ``git`` double that records every command and can fail one subcommand."""

    def __init__(self, fail: str | None = None) -> None:
        """Remember which subcommand (``add``, ``commit`` or ``push``) should exit non-zero."""
        self.fail = fail
        self.calls: list[ProcessCall] = []

    def __call__(self, call: ProcessCall) -> ProcessResult:
        """Record the call and answer with success, or with status 1 for the failing subcommand."""
        self.calls.append(call)
        return ProcessResult(1, "", "refused") if call.argv[1] == self.fail else ProcessResult(0)


def registry_of(tmp_path: Path, *packages: str, extra: dict[str, str] | None = None) -> Path:
    """Build a registry in ``tmp_path/registry`` holding the real trackers of *packages* and a fresh ``_registry.toml``."""
    root = tmp_path / "registry"
    root.mkdir()
    for package in packages:
        shutil.copytree(SNAPSHOT / package / "_check_updates", root / package / "_check_updates")
    for name, text in (extra or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    write_registry_file(root, {"format": 2, "name": "test-registry", "description": "A registry for tests."})
    return root


def machine(tmp_path: Path, *cases: str, artifacts: dict[str, bytes] | None = None, git: Git | None = None) -> FakeSystem:
    """A FakeSystem serving the fixture *cases* and the *artifacts* ``{url: bytes}``, with *git* as the only ``git``."""
    system = FakeSystem(tmp_path / "machine", processes={"git": git} if git else {})
    for case in cases:
        system.urls.update(load_case(case))
    for url, body in (artifacts or {}).items():
        system.urls[url] = FakeHttp(body)
    return system


def zip_bytes(tmp_path: Path, name: str, members: dict[str, str]) -> bytes:
    """Return the bytes of a zip archive of *members*."""
    return make_zip(tmp_path / name, members).read_bytes()


def snapshot_files(root: Path) -> dict[str, bytes]:
    """Return ``{relative path: bytes}`` of every file below *root*."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def patch_digest(system: FakeSystem, old: str, new: str) -> None:
    """Replace the digest *old* by *new* in every served text response (the artifact then matches what the vendor published)."""
    for url, response in list(system.urls.items()):
        if isinstance(response.body, str) and old in response.body:
            system.urls[url] = FakeHttp(response.body.replace(old, new), response.status, response.headers)


def run_update(root: Path, system: FakeSystem, tmp_path: Path, *names: str):
    """Run ``update_registry`` with a sandbox beside the registry and no environment."""
    return update_registry(root, system, sandbox=tmp_path / "sandbox", names=list(names) or None, environ={})


def test_a_hash_the_vendor_does_not_publish_is_computed_marked_and_fingerprinted(tmp_path: Path) -> None:
    """HWiNFO publishes no checksum: the new version carries the tool's own hash, flagged for review, with a matching fingerprint."""
    body = zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)
    root = registry_of(tmp_path, "hwinfo")
    system = machine(tmp_path, "page_hwinfo", artifacts={"https://www.hwinfo.com/files/hwi_1002.zip": body})

    report = run_update(root, system, tmp_path, "hwinfo")

    outcome = report.outcomes[0]
    assert (outcome.status, outcome.version, outcome.review) == ("added", "10.02", True)
    version = root / "hwinfo" / "10.02"
    manifest = parse_manifest((version / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.VERSION)
    assert manifest.source.sha256 == hashlib.sha256(body).hexdigest()
    assert manifest.source.sha256_from == "registry"
    assert manifest.source.download_url == "https://www.hwinfo.com/files/hwi_1002.zip"
    assert sorted(p.name for p in version.iterdir()) == ["gupkg-tui.cmd", "install.cmd", "pkg.toml"]
    settings = read_registry_file(root)
    assert settings["fingerprints"]["hwinfo/10.02"] == fingerprint_directory(version)
    assert check_directory(version).ok and not check_directory(version).warnings
    assert validate_registry(root).problems == ()


def test_a_discovered_checksum_is_verified_and_recorded_as_the_vendors(tmp_path: Path) -> None:
    """With a GitHub digest the artifact is verified against it and ``sha256_from`` stays ``github-digest``; nothing is flagged."""
    body = zip_bytes(tmp_path, "v.zip", VOLTA_ZIP)
    digest = hashlib.sha256(body).hexdigest()
    root = registry_of(tmp_path, "volta")
    system = machine(tmp_path, "github_volta", artifacts={"https://github.com/o/r/releases/download/v2.0.1/volta-2.0.1-windows.zip": body})
    patch_digest(system, "9ce45e2deeb806d62a504075f992a4c5de66873173c2e403a49a3de9126fa5d3", digest)

    outcome = run_update(root, system, tmp_path, "volta").outcomes[0]

    assert (outcome.status, outcome.review) == ("added", False)
    manifest = parse_manifest((root / "volta" / "2.0.1" / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.VERSION)
    assert (manifest.source.sha256, manifest.source.sha256_from) == (digest, "github-digest")


def test_an_artifact_that_does_not_match_the_published_digest_writes_nothing(tmp_path: Path) -> None:
    """A wrong artifact is a failed outcome with no new directory, no leftover and an untouched ``_registry.toml``."""
    root = registry_of(tmp_path, "volta")
    system = machine(tmp_path, "github_volta", artifacts={
        "https://github.com/o/r/releases/download/v2.0.1/volta-2.0.1-windows.zip": zip_bytes(tmp_path, "v.zip", VOLTA_ZIP)
    })
    before = snapshot_files(root)

    outcome = run_update(root, system, tmp_path, "volta").outcomes[0]

    assert outcome.status == "failed" and "SHA-256" in outcome.message
    assert snapshot_files(root) == before
    assert sorted(p.name for p in (root / "volta").iterdir()) == ["_check_updates"]


def test_every_older_version_is_kept_and_a_known_version_is_left_alone(tmp_path: Path) -> None:
    """An existing version directory stays byte for byte; a second run finds the version current and changes nothing."""
    body = zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)
    root = registry_of(tmp_path, "hwinfo")
    old = root / "hwinfo" / "9.99"
    old.mkdir()
    (old / "pkg.toml").write_text(
        'format = 2\nmodes = ["system", "user"]\n[source]\ndownload_url = "https://www.hwinfo.com/files/hwi_999.zip"\n'
        f'sha256 = "{"a" * 64}"\nsha256_from = "manual"\nshipped = false\n', encoding="utf-8", newline="\n",
    )
    kept = (old / "pkg.toml").read_bytes()
    system = machine(tmp_path, "page_hwinfo", artifacts={"https://www.hwinfo.com/files/hwi_1002.zip": body})

    first = run_update(root, system, tmp_path, "hwinfo")
    after_first = snapshot_files(root)
    second = run_update(root, system, tmp_path, "hwinfo")

    assert first.outcomes[0].status == "added" and (old / "pkg.toml").read_bytes() == kept
    assert second.outcomes[0].status == "current"
    assert snapshot_files(root) == after_first


def test_the_staged_app_lives_in_the_sandbox_and_is_gone_afterwards(tmp_path: Path) -> None:
    """Unpack and prepare run below the sandbox folder; the registry never holds an ``App`` and the stage is removed."""
    root = registry_of(tmp_path, "hwinfo")
    system = machine(tmp_path, "page_hwinfo", artifacts={"https://www.hwinfo.com/files/hwi_1002.zip": zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)})

    run_update(root, system, tmp_path, "hwinfo")

    assert not list(root.rglob("App"))
    stage = tmp_path / "sandbox" / "stage"
    assert not stage.exists() or not any(stage.iterdir())
    assert any((tmp_path / "sandbox" / "cache").rglob("*")), "the download cache is in the sandbox"


def test_a_package_that_fails_does_not_block_the_others(tmp_path: Path) -> None:
    """One package's unpack failure is reported for it alone; the other package is written."""
    root = registry_of(tmp_path, "hwinfo", "volta")
    good = zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)
    system = machine(tmp_path, "page_hwinfo", "github_volta", artifacts={
        "https://www.hwinfo.com/files/hwi_1002.zip": good,
        "https://github.com/o/r/releases/download/v2.0.1/volta-2.0.1-windows.zip": b"this is not an archive",
    })
    patch_digest(system, "9ce45e2deeb806d62a504075f992a4c5de66873173c2e403a49a3de9126fa5d3", hashlib.sha256(b"this is not an archive").hexdigest())

    report = run_update(root, system, tmp_path)

    status = {o.package: o.status for o in report.outcomes}
    assert status == {"hwinfo": "added", "volta": "failed"} and not report.ok
    assert not (root / "volta" / "2.0.1").exists()


def test_an_unknown_package_name_is_a_failed_outcome(tmp_path: Path) -> None:
    """Naming a package the registry does not have fails that name only."""
    root = registry_of(tmp_path, "hwinfo")

    outcome = run_update(root, machine(tmp_path), tmp_path, "nope").outcomes[0]

    assert (outcome.package, outcome.status) == ("nope", "failed")


def test_a_download_over_plain_http_without_a_published_hash_is_refused(tmp_path: Path) -> None:
    """The registry's own hash is only ever computed over ``https://``: an ``http://`` address fails and writes nothing."""
    tracker = (
        'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n[track]\nprovider = "page"\n'
        'check_url = "https://example.org/dl"\nmatch = \'tool-(?P<version>\\d+\\.\\d+)\\.zip\'\n'
        '[source]\ndownload_url = "http://example.org/tool-${version}.zip"\n'
    )
    root = registry_of(tmp_path, extra={"plain/_check_updates/pkg.toml": tracker})
    system = machine(tmp_path, artifacts={"http://example.org/tool-1.5.zip": zip_bytes(tmp_path, "t.zip", {"a.exe": "a"})})
    system.urls["https://example.org/dl"] = FakeHttp("<a>tool-1.5.zip</a>")

    outcome = run_update(root, system, tmp_path, "plain").outcomes[0]

    assert outcome.status == "failed"
    assert not (root / "plain" / "1.5").exists()
    assert "http://example.org/tool-1.5.zip" not in [r.url for r in system.request_log]


def test_a_redirect_to_plain_http_is_refused_when_the_tool_computes_the_hash(tmp_path: Path) -> None:
    """A computed hash needs every hop to be https: an https address that redirects to http fails."""
    root = registry_of(tmp_path, "hwinfo")
    system = machine(tmp_path, "page_hwinfo")
    system.urls["https://www.hwinfo.com/files/hwi_1002.zip"] = FakeHttp(b"", 302, {"Location": "http://evil.example/hwi.zip"})
    system.urls["http://evil.example/hwi.zip"] = FakeHttp(zip_bytes(tmp_path, "h.zip", HWINFO_ZIP))

    outcome = run_update(root, system, tmp_path, "hwinfo").outcomes[0]

    assert outcome.status == "failed"
    assert not (root / "hwinfo" / "10.02").exists()


def test_support_files_are_copied_but_no_update_check_code_launcher_or_backup(tmp_path: Path) -> None:
    """Icons and helper modules reach the version; a module that defines ``track``, a stale launcher and a backup do not (launchers are generated)."""
    tracker = (
        'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n[track]\nprovider = "page"\n'
        'check_url = "https://example.org/dl"\nmatch = \'tool-(?P<version>\\d+\\.\\d+)\\.zip\'\n'
        '[source]\ndownload_url = "https://example.org/tool-${version}.zip"\n'
    )
    root = registry_of(tmp_path, extra={
        "tool/_check_updates/pkg.toml": tracker,
        "tool/_check_updates/icons/tool.ico": "ico",
        "tool/_check_updates/pkg.local/helper.py": "PKG_MODULE_API = 2\nVALUE = 1\n",
        "tool/_check_updates/pkg.local/old_track.py": "PKG_MODULE_API = 2\n\n\ndef track(ctx):\n    return None\n",
        "tool/_check_updates/gupkg-tui.cmd": "stale launcher",
        "tool/_check_updates/notes.bak": "backup",
    })
    system = machine(tmp_path, artifacts={"https://example.org/tool-1.5.zip": zip_bytes(tmp_path, "t.zip", {"a.exe": "a"})})
    system.urls["https://example.org/dl"] = FakeHttp("<a>tool-1.5.zip</a>")

    outcome = run_update(root, system, tmp_path, "tool").outcomes[0]

    assert outcome.status == "added", outcome.message
    version = root / "tool" / "1.5"
    assert sorted(p.relative_to(version).as_posix() for p in version.rglob("*") if p.is_file()) == [
        "gupkg-tui.cmd", "icons/tool.ico", "install.cmd", "pkg.local/helper.py", "pkg.toml",
    ]
    assert (version / "gupkg-tui.cmd").read_bytes() == launcher_text("tui").encode("ascii")


def test_a_tracker_that_does_not_validate_is_reported_and_skipped(tmp_path: Path) -> None:
    """A tracker with an error is a failed outcome for its package, with the checker's message."""
    root = registry_of(tmp_path, extra={"broken/_check_updates/pkg.toml": 'format = 2\n[track]\nprovider = "nope"\n'})

    outcome = run_update(root, machine(tmp_path), tmp_path, "broken").outcomes[0]

    assert outcome.status == "failed" and "does not validate" in outcome.message


def test_a_written_version_directory_verifies_as_a_distribution_copy_and_is_refused_once_changed(tmp_path: Path) -> None:
    """A copy of the tool's output matches the published fingerprint; changing one file of the copy is detected and named."""
    root = registry_of(tmp_path, "hwinfo")
    system = machine(tmp_path, "page_hwinfo", artifacts={"https://www.hwinfo.com/files/hwi_1002.zip": zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)})
    run_update(root, system, tmp_path, "hwinfo")
    published = read_registry_file(root)["fingerprints"]["hwinfo/10.02"]
    copy = tmp_path / "dist" / "hwinfo" / "10.02"
    shutil.copytree(root / "hwinfo" / "10.02", copy)

    assert verify_directory(copy, published, reference=root / "hwinfo" / "10.02") == []

    (copy / "pkg.toml").write_bytes((copy / "pkg.toml").read_bytes().replace(b"HWiNFO64.exe", b"evil.exe"))
    problems = verify_directory(copy, published, reference=root / "hwinfo" / "10.02")

    assert "differs: pkg.toml (changed)" in problems


# -- pin ------------------------------------------------------------------------------------------------


LEGACY = (
    'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n[source]\n'
    'download_url = "https://example.org/old-1.0.zip"\nkind = "zip"\nshipped = false\n'
)


def legacy_registry(tmp_path: Path, text: str = LEGACY) -> Path:
    """A registry with one legacy version ``old/1.0`` that has no hash and is listed as unpinned."""
    root = registry_of(tmp_path, extra={"old/1.0/pkg.toml": text})
    write_launchers(root / "old" / "1.0")
    write_registry_file(root, {
        "format": 2, "name": "t", "description": "t", "unpinned": ["old/1.0"],
        "fingerprints": {"old/1.0": fingerprint_directory(root / "old" / "1.0")},
    })
    return root


def test_pin_gives_a_legacy_version_its_first_hash_and_a_new_fingerprint(tmp_path: Path) -> None:
    """The observed hash is written as the registry's own, the fingerprint is recomputed and the version leaves ``unpinned``."""
    body = zip_bytes(tmp_path, "o.zip", {"a.exe": "a"})
    root = legacy_registry(tmp_path)
    system = machine(tmp_path, artifacts={"https://example.org/old-1.0.zip": body})
    before = read_registry_file(root)["fingerprints"]["old/1.0"]

    outcome = pin_version(root, system, "old/1.0", sandbox=tmp_path / "sandbox", environ={})

    assert (outcome.status, outcome.review) == ("pinned", True)
    manifest = parse_manifest((root / "old" / "1.0" / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.VERSION)
    assert (manifest.source.sha256, manifest.source.sha256_from) == (hashlib.sha256(body).hexdigest(), "registry")
    settings = read_registry_file(root)
    assert settings["fingerprints"]["old/1.0"] == fingerprint_directory(root / "old" / "1.0") != before
    assert "unpinned" not in settings


def test_pin_never_replaces_an_existing_hash(tmp_path: Path) -> None:
    """A version that already has a ``sha256`` is refused and left untouched."""
    root = legacy_registry(tmp_path, LEGACY + f'sha256 = "{"b" * 64}"\nsha256_from = "manual"\n')
    before = snapshot_files(root)

    outcome = pin_version(root, machine(tmp_path), "old/1.0", sandbox=tmp_path / "sandbox", environ={})

    assert outcome.status == "failed" and "already has a sha256" in outcome.message
    assert snapshot_files(root) == before


@pytest.mark.parametrize("key", ["old/9.9", "nope/1.0", "old", "../x/1.0"], ids=["no-version", "no-package", "no-slash", "escape"])
def test_pin_refuses_a_version_that_is_not_a_directory_of_the_registry(tmp_path: Path, key: str) -> None:
    """Only an existing ``name/version`` directory can be pinned; other names fail without writing."""
    root = legacy_registry(tmp_path)
    before = snapshot_files(root)

    outcome = pin_version(root, machine(tmp_path), key, sandbox=tmp_path / "sandbox", environ={})

    assert outcome.status == "failed"
    assert snapshot_files(root) == before


# -- git and the command line -------------------------------------------------------------------------------


def run_cli(root: Path, system: FakeSystem, tmp_path: Path, *arguments: str) -> int:
    """Run the command line against *root* with the given system and an empty environment."""
    return main([*arguments, "--registry", str(root), "--sandbox", str(tmp_path / "sandbox")], system=system, environ={})


def git_words(git: Git) -> list[list[str]]:
    """Return the commands the git double saw, without the program name."""
    return [list(call.argv[1:]) for call in git.calls]


def test_without_auto_push_the_tool_starts_no_git_process(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``update`` writes the version, says it is uncommitted, and no process named git is ever started."""
    root = registry_of(tmp_path, "hwinfo")
    git = Git()
    system = machine(tmp_path, "page_hwinfo", artifacts={"https://www.hwinfo.com/files/hwi_1002.zip": zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)}, git=git)

    status = run_cli(root, system, tmp_path, "update", "hwinfo")

    assert status == 0 and git.calls == []
    assert not any("git" in Path(command[0]).name.lower() for command in system.process_log)
    output = capsys.readouterr().out
    assert "not committed" in output and "[REVIEW]" in output


def hwinfo_world(tmp_path: Path, git: Git) -> tuple[Path, FakeSystem]:
    """A registry with the HWiNFO tracker (its hash is computed by the tool) and a system that serves it, with *git* as the only git."""
    root = registry_of(tmp_path, "hwinfo")
    body = zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)
    return root, machine(tmp_path, "page_hwinfo", artifacts={"https://www.hwinfo.com/files/hwi_1002.zip": body}, git=git)


def test_auto_push_commits_only_what_the_run_wrote_and_pushes_once(tmp_path: Path) -> None:
    """With ``--auto-push --allow-computed``: add and commit name the new directory and ``_registry.toml`` as pathspecs, then one plain push."""
    git = Git()
    root, system = hwinfo_world(tmp_path, git)

    status = run_cli(root, system, tmp_path, "update", "hwinfo", "--auto-push", "--allow-computed")

    assert status == 0
    words = git_words(git)
    assert [w[0] for w in words] == ["add", "commit", "push"]
    literal = [":(literal)hwinfo/10.02", ":(literal)_registry.toml"]  # a version name is never read as a pattern
    assert words[0] == ["add", "--", *literal]
    assert words[1][:2] == ["commit", "-m"] and words[1][3:] == ["--", *literal]
    assert words[2] == ["push"]
    assert all(call.cwd == root for call in git.calls)
    assert "hwinfo 10.02" in words[1][2] and "computed by the registry tool" in words[1][2]
    flat = sum(words, [])
    assert not {"--no-verify", "--force", "-f", "--force-with-lease", "-A", "--all"} & set(flat)


def test_auto_push_refuses_hashes_the_tool_computed_itself_unless_allowed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Trust-on-first-use hashes are never published unreviewed: no git process starts, the status is 1, the version stays written."""
    git = Git()
    root, system = hwinfo_world(tmp_path, git)

    status = run_cli(root, system, tmp_path, "update", "hwinfo", "--auto-push")

    assert status == 1 and git.calls == []
    assert "refusing to publish hashes the registry tool computed itself" in capsys.readouterr().err
    assert (root / "hwinfo" / "10.02" / "pkg.toml").is_file()


def test_auto_push_publishes_vendor_verified_hashes_without_the_flag(tmp_path: Path) -> None:
    """A version whose hash was verified against the vendor's checksum needs no ``--allow-computed``."""
    git = Git()
    root = registry_of(tmp_path, "volta")
    body = zip_bytes(tmp_path, "v.zip", VOLTA_ZIP)
    system = machine(tmp_path, "github_volta", artifacts={"https://github.com/o/r/releases/download/v2.0.1/volta-2.0.1-windows.zip": body}, git=git)
    patch_digest(system, "9ce45e2deeb806d62a504075f992a4c5de66873173c2e403a49a3de9126fa5d3", hashlib.sha256(body).hexdigest())

    status = run_cli(root, system, tmp_path, "update", "volta", "--auto-push")

    assert status == 0 and [w[0] for w in git_words(git)] == ["add", "commit", "push"]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        pytest.param(["--remote", "mirror"], ["push", "mirror"], id="remote-only"),
        pytest.param(["--branch", "stable"], ["push", "origin", "HEAD:stable"], id="branch-only"),
        pytest.param(["--remote", "mirror", "--branch", "stable"], ["push", "mirror", "HEAD:stable"], id="both"),
    ],
)
def test_the_push_goes_where_it_is_told_and_never_forces(tmp_path: Path, options: list[str], expected: list[str]) -> None:
    """``--remote`` and ``--branch`` choose the push target; the push has no force option."""
    git = Git()
    root, system = hwinfo_world(tmp_path, git)

    status = run_cli(root, system, tmp_path, "update", "hwinfo", "--auto-push", "--allow-computed", *options)

    assert status == 0 and git_words(git)[2] == expected


@pytest.mark.parametrize("option", ["--remote", "--branch"])
@pytest.mark.parametrize(
    "value",
    [
        "-x", "--upload-pack=calc", "a b", "", "a..b", "/x", "x/", ".x", "x.", "x.lock", "a@{b", "a\\b", "a~b", "a^b", "a:b",
        "a?b", "a*b", "a[b", "a//b", "a\x01b", "a\x7fb", "@", "x/.y", "x/y.lock",
    ],
    ids=lambda value: ascii(value),
)
def test_an_unsafe_remote_or_branch_name_is_refused_before_git_starts(tmp_path: Path, option: str, value: str) -> None:
    """A name that git could read as an option is refused; nothing is added, committed or pushed."""
    git = Git()
    root, system = hwinfo_world(tmp_path, git)

    status = run_cli(root, system, tmp_path, "update", "hwinfo", "--auto-push", "--allow-computed", f"{option}={value}")

    assert status == 1 and git.calls == []


def test_a_branch_may_not_be_a_full_reference_but_ordinary_names_pass(tmp_path: Path) -> None:
    """``refs/heads/x`` is not a branch name for ``--branch`` (it would name another namespace); ``release/1.x`` is fine."""
    git = Git()
    root, system = hwinfo_world(tmp_path, git)
    assert run_cli(root, system, tmp_path, "update", "hwinfo", "--auto-push", "--allow-computed", "--branch=refs/heads/x") == 1
    assert git.calls == []

    (tmp_path / "again").mkdir()
    git = Git()
    root, system = hwinfo_world(tmp_path / "again", git)
    status = run_cli(root, system, tmp_path / "again", "update", "hwinfo", "--auto-push", "--allow-computed", "--branch=release/1.x", "--remote=my-mirror_2")

    assert status == 0 and git_words(git)[2] == ["push", "my-mirror_2", "HEAD:release/1.x"]


def test_auto_push_with_nothing_new_runs_no_git(tmp_path: Path) -> None:
    """When every version is current there is nothing to commit and git is not started."""
    git = Git()
    root, system = hwinfo_world(tmp_path, git)
    run_cli(root, system, tmp_path, "update", "hwinfo")

    status = run_cli(root, system, tmp_path, "update", "hwinfo", "--auto-push", "--allow-computed")

    assert status == 0 and git.calls == []


@pytest.mark.parametrize("failing", ["add", "commit", "push"])
def test_a_failing_git_step_fails_the_run_and_stops_there(tmp_path: Path, failing: str, capsys: pytest.CaptureFixture[str]) -> None:
    """If add, commit or push exits non-zero the status is 1, the error is printed and later steps do not run."""
    git = Git(fail=failing)
    root, system = hwinfo_world(tmp_path, git)

    status = run_cli(root, system, tmp_path, "update", "hwinfo", "--auto-push", "--allow-computed")

    assert status == 1
    assert [w[0] for w in git_words(git)] == ["add", "commit", "push"][: ["add", "commit", "push"].index(failing) + 1]
    assert "failed with status 1" in capsys.readouterr().err


def test_the_exit_status_is_one_when_a_package_fails_and_two_without_a_registry_file(tmp_path: Path) -> None:
    """A failing package makes status 1; a folder with no ``_registry.toml`` is a usage error (2) that writes nothing."""
    root = registry_of(tmp_path, "hwinfo")
    assert run_cli(root, machine(tmp_path), tmp_path, "update", "hwinfo") == 1  # the vendor page is not served

    bare = tmp_path / "bare"
    (bare / "x").mkdir(parents=True)
    assert run_cli(bare, machine(tmp_path), tmp_path, "update") == 2
    assert not (bare / "_registry.toml").exists()


def test_the_commit_message_names_every_hash_the_tool_computed(tmp_path: Path) -> None:
    """The message separates reviewed-by-vendor hashes from the registry's own, so a maintainer sees what to check."""
    computed = Outcome("a", "added", "1.0", review=True, path=tmp_path)
    verified = Outcome("b", "added", "2.0", path=tmp_path)

    assert 'a 1.0 (sha256_from = "registry")' in commit_message([computed, verified])
    assert "Every hash was verified" in commit_message([verified])


def test_every_fixture_case_names_a_tracker_that_exists() -> None:
    """The fixture cases this module relies on are present (a renamed case fails here, not in a distant test)."""
    for name in ("page_hwinfo", "github_volta"):
        document = json.loads((HTTP_CASES / f"{name}.json").read_text(encoding="utf-8"))
        assert (SNAPSHOT / document["tracker"] / "_check_updates" / "pkg.toml").is_file()


# -- housekeeping ---------------------------------------------------------------------------------------------


def test_a_run_removes_the_staging_folders_an_interrupted_run_left_but_nothing_else(tmp_path: Path) -> None:
    """Old folders named exactly ``.new-`` and eight hex digits are the tool's own and are removed; young ones (another run) and other names stay."""
    root = registry_of(tmp_path, "hwinfo")
    old = root / "hwinfo" / ".new-0123abcd"
    old.mkdir()
    (old / "pkg.toml").write_text("x", encoding="utf-8")
    long_ago = time.time() - 2 * 3600
    os.utime(old, (long_ago, long_ago))
    (root / "hwinfo" / ".new-fedcba98").mkdir()  # a live run's folder: created just now
    (root / "hwinfo" / ".new-keep").mkdir()
    (root / "hwinfo" / ".new-0123abcdef").mkdir()

    run_update(root, machine(tmp_path), tmp_path, "hwinfo")

    assert sorted(p.name for p in (root / "hwinfo").iterdir()) == [".new-0123abcdef", ".new-fedcba98", ".new-keep", "_check_updates"]


def test_a_staging_folder_that_cannot_be_removed_is_a_warning_not_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An error while cleaning one folder is reported and the run goes on with the packages."""
    root = registry_of(tmp_path, "hwinfo")
    stale = root / "hwinfo" / ".new-0123abcd"
    stale.mkdir()
    os.utime(stale, (1_000_000, 1_000_000))

    def refuse(path: Path) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr("gupkg_registry.update.remove_tree", refuse)
    reporter = Collect()
    with use_reporter(reporter):
        report = run_update(root, machine(tmp_path), tmp_path, "hwinfo")

    assert stale.is_dir() and [o.package for o in report.outcomes] == ["hwinfo"]
    assert any(isinstance(e, Warning) and "in use" in e.text for e in reporter.events)


def test_registry_files_are_written_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write that fails at the last step leaves the old file whole and no temporary file behind."""
    root = registry_of(tmp_path)
    before = (root / "_registry.toml").read_bytes()

    def fail(source: object, destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("gupkg_registry.layout.os.replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_registry_file(root, {"format": 2, "name": "other", "description": "other"})

    assert (root / "_registry.toml").read_bytes() == before
    assert [p.name for p in root.iterdir()] == ["_registry.toml"]


def test_the_launchers_command_brings_a_registry_to_the_current_template_and_refreshes_fingerprints(tmp_path: Path) -> None:
    """`launchers` rewrites a stale version launcher, adds the tracker's check-updates.cmd, drops a tracker's install.cmd, fixes the fingerprint, and a second run changes nothing."""
    root = tmp_path / "registry"
    shutil.copytree(SNAPSHOT, root)
    version = root / "qbittorrent" / "5.2.3"
    tracker = root / "gh" / "_check_updates"
    (version / "install.cmd").write_bytes(b"@echo off\r\nrem the 0.17 launcher\r\n")
    (tracker / "check-updates.cmd").unlink()
    (tracker / "install.cmd").write_bytes(b"@echo off\r\n")
    assert validate_registry(root).problems

    assert main(["launchers", "--registry", str(root)]) == 0

    assert validate_registry(root).problems == ()
    assert (version / "install.cmd").read_bytes() == launcher_text("install").encode("ascii")
    assert (tracker / "check-updates.cmd").read_bytes() == launcher_text("check-updates").encode("ascii")
    assert not (tracker / "install.cmd").exists()
    assert read_registry_file(root)["fingerprints"]["qbittorrent/5.2.3"] == fingerprint_directory(version)
    before = (root / "_registry.toml").read_bytes()
    assert main(["launchers", "--registry", str(root)]) == 0
    assert (root / "_registry.toml").read_bytes() == before


def test_update_prints_a_progress_line_per_package_and_a_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each package gets ``[n/total] name ... <outcome>`` on one line as it finishes, and the run ends with a summary."""
    root = registry_of(tmp_path, "hwinfo", "volta")
    good = zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)
    system = machine(tmp_path, "page_hwinfo", "github_volta", artifacts={
        "https://www.hwinfo.com/files/hwi_1002.zip": good,
        "https://github.com/o/r/releases/download/v2.0.1/volta-2.0.1-windows.zip": b"this is not an archive",
    })
    patch_digest(system, "9ce45e2deeb806d62a504075f992a4c5de66873173c2e403a49a3de9126fa5d3", hashlib.sha256(b"this is not an archive").hexdigest())

    status = run_cli(root, system, tmp_path, "update")

    lines = capsys.readouterr().out.splitlines()
    assert status == 1
    assert lines[0].startswith("[1/2] hwinfo ... added") and lines[1].startswith("[2/2] volta ... failed")
    assert "summary: 1 added, 1 failed of 2 packages" in lines


def test_a_written_version_states_its_modes_even_when_they_are_the_default(tmp_path: Path) -> None:
    """The registry requires every package to state ``modes``, so the pinned manifest is written with them explicitly."""
    text = 'format = 2\ndescription = "d"\n[source]\ndownload_url = "https://example.com/a.zip"\n'
    manifest = parse_manifest(text, ManifestKind.VERSION)

    assert "modes" not in manifest_to_toml(manifest)
    explicit = parse_manifest(manifest_to_toml(manifest, explicit_modes=True), ManifestKind.VERSION)
    assert "\nmodes = [" in manifest_to_toml(manifest, explicit_modes=True)
    assert explicit.modes == manifest.modes


def _write_lock(root: Path, pid: int, *, operation: str = "registry tool update") -> Path:
    """Write a registry lock file as another run on this machine would (the started time is now)."""
    lock = root / ".registry-tool.lock"
    holder = Holder(pid, socket.gethostname(), operation, "2026-10-25T01:02:03+00:00", "othertoken")
    lock.write_text(render_lock(holder), encoding="utf-8")
    return lock


def test_a_second_run_is_refused_while_a_live_run_holds_the_lock_and_the_message_names_it(tmp_path: Path) -> None:
    """A live holder stops the run before anything is written, and the error names its process, operation and start time."""
    root, system = hwinfo_world(tmp_path, Git())
    system.live_pids.add(4242)
    lock = _write_lock(root, 4242)

    with pytest.raises(RegistryFileError) as error:
        run_update(root, system, tmp_path)

    assert "process 4242" in str(error.value) and "registry tool update" in str(error.value) and "2026-10-25T01:02:03" in str(error.value)
    assert lock.is_file() and not (root / "hwinfo" / "1.0.0").exists()


def test_a_lock_left_by_a_run_that_has_ended_is_taken_over(tmp_path: Path) -> None:
    """A lock whose process is gone (a crash) is replaced and the run goes ahead, with nothing to delete by hand."""
    root, system = hwinfo_world(tmp_path, Git())
    _write_lock(root, 999999)

    report = run_update(root, system, tmp_path)

    assert [o.status for o in report.outcomes] == ["added"]
    assert not (root / ".registry-tool.lock").exists()


def test_pin_takes_the_same_lock_as_update(tmp_path: Path) -> None:
    """``pin`` is refused while a live run holds the registry lock."""
    root, system = hwinfo_world(tmp_path, Git())
    system.live_pids.add(4242)
    _write_lock(root, 4242, operation="registry tool update")

    with pytest.raises(RegistryFileError, match="in use by process 4242"):
        pin_version(root, system, "hwinfo/1.0.0", sandbox=tmp_path / "sb")


def test_the_summary_lists_the_packages_by_outcome(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """After the progress lines the run prints who was updated, who needs review and who failed with the reason."""
    root = registry_of(tmp_path, "hwinfo", "volta")
    good = zip_bytes(tmp_path, "h.zip", HWINFO_ZIP)
    system = machine(tmp_path, "page_hwinfo", "github_volta", artifacts={
        "https://www.hwinfo.com/files/hwi_1002.zip": good,
        "https://github.com/o/r/releases/download/v2.0.1/volta-2.0.1-windows.zip": b"this is not an archive",
    })
    patch_digest(system, "9ce45e2deeb806d62a504075f992a4c5de66873173c2e403a49a3de9126fa5d3", hashlib.sha256(b"this is not an archive").hexdigest())

    run_cli(root, system, tmp_path, "update")

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("updated (1): hwinfo ") for line in lines)
    assert any(line.startswith("hash computed by the registry tool, review before pushing (1): hwinfo") for line in lines)
    assert "failed (1):" in lines and any(line.startswith("  volta: ") for line in lines)


def test_the_lock_is_released_after_a_run(tmp_path: Path) -> None:
    """After a run, normal or failed, the lock file is gone so the next run can start."""
    root, system = hwinfo_world(tmp_path, Git())

    run_update(root, system, tmp_path)

    assert not (root / ".registry-tool.lock").exists()


def test_a_package_that_runs_a_vendor_installer_is_hashed_but_the_installer_is_never_run(tmp_path: Path) -> None:
    """The tool downloads and hashes the installer for the pinned version; it does not unpack or run it, so `require` is not judged."""
    tracker = (
        'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n[track]\nprovider = "page"\n'
        'check_url = "https://example.org/dl"\nmatch = \'tool_(?P<version>\\d+\\.\\d+)_setup\\.exe\'\n'
        '[source]\ndownload_url = "https://example.org/tool_${version}_setup.exe"\nkind = "exe"\nrequire = ["Compiler/tool.exe"]\n'
        '[source.installer]\nrun = ["/silent", "/installto", "$Stage"]\n'
    )
    root = registry_of(tmp_path, extra={"inst/_check_updates/pkg.toml": tracker})
    system = machine(tmp_path, artifacts={"https://example.org/tool_2.5_setup.exe": b"MZ not really an installer"})
    system.urls["https://example.org/dl"] = FakeHttp("<a>tool_2.5_setup.exe</a>")

    outcome = run_update(root, system, tmp_path, "inst").outcomes[0]

    text = (root / "inst" / "2.5" / "pkg.toml").read_text(encoding="utf-8")
    assert outcome.status == "added" and outcome.review
    assert hashlib.sha256(b"MZ not really an installer").hexdigest() in text and "[source.installer]" in text
    assert system.process_log == []
    assert [problem for problem in validate_registry(root).problems if "inst/2.5" in problem] == []

