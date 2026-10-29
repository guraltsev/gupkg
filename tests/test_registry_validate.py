"""Cover registry validation (what CI runs): each rule rejects what it is meant to reject.

Behavior covered: a sound registry has no problem; each of these is found and named: a missing
``modes``, a version with no ``sha256`` (unless listed under ``unpinned``, and then only while it has
none), a missing or wrong fingerprint, a fingerprint of nothing, a changed or missing launcher, a
launcher in a tracker, a tracker that fails the package checker (an error or only a warning),
``ignore_checksum``, CR line endings in a text file, a backup file, a link, a hook file with no
``Why no service`` note, more than 13 hook files, a script-populated version, and a malformed
``_registry.toml``. The fingerprint check is also shown to follow a changed file.
Real: the committed registry snapshot copied to a temporary folder, then damaged one way at a time,
and the real checker and fingerprint code. Mocked: nothing. Out of scope: the registry tool that
writes the files (``test_registry_tool.py``) and the real registry (``test_registry_100.py``).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from gupkg.services.fingerprint import fingerprint_directory
from gupkg.system.fake import FakeSystem
from gupkg.launchers import launcher_text, write_launchers
from gupkg_registry.layout import read_registry_file, write_registry_file
from gupkg_registry.validate import MAX_HOOK_FILES, _stray_problems, validate_registry
from tests.http_fixtures import FIXTURES

SNAPSHOT = FIXTURES / "registry_100"
HASH = "c" * 64


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    """A copy of the snapshot with one more fully pinned version, ``hwinfo/10.02``, that validates."""
    root = tmp_path / "registry"
    shutil.copytree(SNAPSHOT, root)
    version = root / "hwinfo" / "10.02"
    version.mkdir()
    (version / "pkg.toml").write_bytes(
        b'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n\n[source]\n'
        b'download_url = "https://www.hwinfo.com/files/hwi_1002.zip"\n'
        + f'sha256 = "{HASH}"\nsha256_from = "registry"\nshipped = false\n'.encode()
    )
    write_launchers(version)
    settings = read_registry_file(root)
    settings["fingerprints"]["hwinfo/10.02"] = fingerprint_directory(version)
    write_registry_file(root, settings)
    return root


def refingerprint(root: Path, key: str) -> None:
    """Record the current fingerprint of *key* (so a test can damage one thing without also breaking the fingerprint)."""
    settings = read_registry_file(root)
    settings["fingerprints"][key] = fingerprint_directory(root / key)
    write_registry_file(root, settings)


def problems_of(root: Path) -> list[str]:
    """Return the validation problems of *root*."""
    return list(validate_registry(root).problems)


def test_a_sound_registry_has_no_problem(registry: Path) -> None:
    """The snapshot plus one fully pinned version validates."""
    report = validate_registry(registry)

    assert report.problems == ()
    assert report.packages == 79 and report.versions == 2


def test_the_fingerprint_follows_every_changed_file(registry: Path) -> None:
    """Changing a byte of a pinned version is reported with the published and the actual fingerprint."""
    manifest = registry / "hwinfo" / "10.02" / "pkg.toml"
    manifest.write_bytes(manifest.read_bytes().replace(b'"d"', b'"e"'))

    found = problems_of(registry)

    assert any(p.startswith("hwinfo/10.02: fingerprint ") and "does not match" in p for p in found)


def drop_modes(root: Path) -> None:
    """Remove the ``modes`` line of the tracker of ``gh``."""
    path = root / "gh" / "_check_updates" / "pkg.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('modes = ["system", "user"]\n', ""), encoding="utf-8", newline="\n")


def drop_sha(root: Path) -> None:
    """Remove the ``sha256`` of ``hwinfo/10.02`` and refingerprint it."""
    path = root / "hwinfo" / "10.02" / "pkg.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(f'sha256 = "{HASH}"\nsha256_from = "registry"\n', ""), encoding="utf-8", newline="\n")
    refingerprint(root, "hwinfo/10.02")


def drop_provenance(root: Path) -> None:
    """Remove ``sha256_from`` from ``hwinfo/10.02`` and refingerprint it."""
    path = root / "hwinfo" / "10.02" / "pkg.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('sha256_from = "registry"\n', ""), encoding="utf-8", newline="\n")
    refingerprint(root, "hwinfo/10.02")


def drop_fingerprint(root: Path) -> None:
    """Remove the fingerprint of ``hwinfo/10.02``."""
    settings = read_registry_file(root)
    del settings["fingerprints"]["hwinfo/10.02"]
    write_registry_file(root, settings)


def stale_fingerprint(root: Path) -> None:
    """Publish a fingerprint for a version that does not exist."""
    settings = read_registry_file(root)
    settings["fingerprints"]["hwinfo/0.0"] = HASH
    write_registry_file(root, settings)


def tamper_launcher(root: Path) -> None:
    """Append a command to ``install.cmd`` of ``hwinfo/10.02`` and refingerprint (so only the template check can notice)."""
    path = root / "hwinfo" / "10.02" / "install.cmd"
    path.write_bytes(path.read_bytes() + b"calc\r\n")
    refingerprint(root, "hwinfo/10.02")


def remove_launcher(root: Path) -> None:
    """Delete ``gupkg-tui.cmd`` of ``hwinfo/10.02`` and refingerprint."""
    (root / "hwinfo" / "10.02" / "gupkg-tui.cmd").unlink()
    refingerprint(root, "hwinfo/10.02")


def launcher_in_tracker(root: Path) -> None:
    """Put a launcher into a tracker."""
    (root / "gh" / "_check_updates" / "install.cmd").write_bytes(launcher_text("install").encode("ascii"))


def remove_tracker_launcher(root: Path) -> None:
    """Delete the tracker's check-updates.cmd."""
    (root / "gh" / "_check_updates" / "check-updates.cmd").unlink()


def tamper_tracker_launcher(root: Path) -> None:
    """Replace the tracker's check-updates.cmd by a hand-written one."""
    (root / "gh" / "_check_updates" / "check-updates.cmd").write_bytes(b"@echo off\r\ncall gupkg install gh\r\n")


def tracker_error(root: Path) -> None:
    """Break a tracker (an unknown provider)."""
    path = root / "gh" / "_check_updates" / "pkg.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('provider = "github"', 'provider = "nope"'), encoding="utf-8", newline="\n")


def tracker_warning_only(root: Path) -> None:
    """Add the old HWiNFO scraper, as an API 2 module: the checker only warns that a built-in provider could do it."""
    hook = root / "gh" / "_check_updates" / "pkg.local"
    hook.mkdir()
    original = (FIXTURES / "hints" / "hwinfo" / "check_update.py").read_text(encoding="utf-8")
    (hook / "scrape.py").write_text("PKG_MODULE_API = 2\n" + original, encoding="utf-8", newline="\n")


def ignore_checksum(root: Path) -> None:
    """Reintroduce the removed bypass in a tracker."""
    path = root / "gh" / "_check_updates" / "pkg.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("[source]\n", "[source]\nignore_checksum = true\n"), encoding="utf-8", newline="\n")


def crlf_toml(root: Path) -> None:
    """Rewrite a tracker with CRLF line endings."""
    path = root / "gh" / "_check_updates" / "pkg.toml"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))


def backup_file(root: Path) -> None:
    """Leave a ``.bak`` file in a tracker."""
    (root / "gh" / "_check_updates" / "pkg.toml.bak").write_bytes(b"old")


def script_version(root: Path) -> None:
    """Make ``hwinfo/10.02`` script-populated."""
    path = root / "hwinfo" / "10.02" / "pkg.toml"
    path.write_bytes(
        b'format = 2\nmodes = ["system", "user"]\n[source]\nscript = "pkg.local/p.py"\nshipped = false\n'
    )
    (path.parent / "pkg.local").mkdir()
    (path.parent / "pkg.local" / "p.py").write_text('"""Why no service: x."""\nPKG_MODULE_API = 2\n\n\ndef populate(ctx):\n    pass\n', encoding="utf-8", newline="\n")
    refingerprint(root, "hwinfo/10.02")


def unpinned_with_hash(root: Path) -> None:
    """List a version that has a hash as unpinned."""
    settings = read_registry_file(root)
    settings["unpinned"] = ["hwinfo/10.02"]
    write_registry_file(root, settings)


def unpinned_nothing(root: Path) -> None:
    """List a version that does not exist as unpinned."""
    settings = read_registry_file(root)
    settings["unpinned"] = ["hwinfo/3.0", "qbittorrent/5.2.3"]
    write_registry_file(root, settings)


def hook_without_note(root: Path) -> None:
    """Add a hook that has no ``Why no service`` note (and does not look like a service)."""
    hook = root / "gh" / "_check_updates" / "pkg.local"
    hook.mkdir()
    (hook / "x.py").write_text('"""Does something."""\nPKG_MODULE_API = 2\nVALUE = 1\n', encoding="utf-8", newline="\n")


def too_many_hooks(root: Path) -> None:
    """Add more hook files than the registry allows (each with a note)."""
    hook = root / "gh" / "_check_updates" / "pkg.local"
    hook.mkdir()
    for index in range(MAX_HOOK_FILES + 1):
        (hook / f"h{index}.py").write_text('"""Why no service: test."""\nPKG_MODULE_API = 2\nVALUE = 1\n', encoding="utf-8", newline="\n")


def link_in_tracker(root: Path) -> None:
    """Put a junction into a tracker (made with a real junction, through ``FakeSystem``)."""
    outside = root.parent / "outside"
    outside.mkdir()
    FakeSystem(root.parent / "machine").create_junction(root / "gh" / "_check_updates" / "link", outside)


def bad_registry_file(root: Path) -> None:
    """Make ``_registry.toml`` say format 1."""
    path = root / "_registry.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("format = 2", "format = 1"), encoding="utf-8", newline="\n")


def no_registry_file(root: Path) -> None:
    """Delete ``_registry.toml``."""
    (root / "_registry.toml").unlink()


def stray_directory(root: Path) -> None:
    """Leave an empty directory in a package folder."""
    (root / "gh" / "junk").mkdir()


def staging_leftover(root: Path) -> None:
    """Leave a staging folder of an interrupted run in a package folder."""
    (root / "gh" / ".new-0123abcd").mkdir()


def hostile_version_name(root: Path) -> None:
    """Add a version directory whose name could expand (``%TEMP%``), with a valid ``pkg.toml``."""
    target = root / "gh" / "%TEMP%"
    shutil.copytree(root / "hwinfo" / "10.02", target)


def stray_file(root: Path) -> None:
    """Leave a file in a package folder."""
    (root / "gh" / "notes.txt").write_text("x", encoding="utf-8")


def unversioned_unknown(root: Path) -> None:
    """List a package that does not exist as unversioned."""
    settings = read_registry_file(root)
    settings["unversioned"] = [*settings.get("unversioned", []), "nope"]
    write_registry_file(root, settings)


def exactly_the_allowed_hooks(root: Path) -> None:
    """Add exactly as many hook files as the registry allows, each with a note (this must pass)."""
    hook = root / "gh" / "_check_updates" / "pkg.local"
    hook.mkdir()
    for index in range(MAX_HOOK_FILES):
        (hook / f"h{index}.py").write_text('"""Why no service: test."""\nPKG_MODULE_API = 2\nVALUE = 1\n', encoding="utf-8", newline="\n")


RULES: list[tuple[str, Callable[[Path], None], str]] = [
    ("stray-directory", stray_directory, "gh/junk: a directory without pkg.toml"),
    ("staging-leftover", staging_leftover, "gh/.new-0123abcd: a directory without pkg.toml"),
    ("hostile-version-name", hostile_version_name, "gh/%TEMP%: not a usable version name"),
    ("stray-file", stray_file, "gh/notes.txt: a file in a package folder"),
    ("unversioned-unknown", unversioned_unknown, "'nope' is listed as unversioned but is not a package"),
    ("missing-modes", drop_modes, "gh/_check_updates: 'modes' is not declared"),
    ("no-sha256", drop_sha, "hwinfo/10.02: has no sha256"),
    ("no-provenance", drop_provenance, "sha256 has no sha256_from"),
    ("no-fingerprint", drop_fingerprint, "hwinfo/10.02: no fingerprint"),
    ("fingerprint-of-nothing", stale_fingerprint, "fingerprint of 'hwinfo/0.0' names no version directory"),
    ("tampered-launcher", tamper_launcher, "hwinfo/10.02/install.cmd: differs from the launcher template"),
    ("missing-launcher", remove_launcher, "hwinfo/10.02/gupkg-tui.cmd: launcher is missing"),
    ("launcher-in-tracker", launcher_in_tracker, "a tracker never installs, so it carries no install.cmd"),
    ("missing-check-updates-launcher", remove_tracker_launcher, "gh/_check_updates/check-updates.cmd: launcher is missing"),
    ("tampered-check-updates-launcher", tamper_tracker_launcher, "gh/_check_updates/check-updates.cmd: differs from the launcher template"),
    ("tracker-error", tracker_error, "gh/_check_updates:"),
    ("tracker-warning", tracker_warning_only, "gh/_check_updates: warning: pkg.local/scrape.py looks like the built-in service"),
    ("ignore-checksum", ignore_checksum, "gh/_check_updates:"),
    ("crlf-text", crlf_toml, "has CR line endings"),
    ("backup-file", backup_file, "backup files do not belong"),
    ("script-populated-version", script_version, "populated by the built-in download, not a script"),
    ("unpinned-but-hashed", unpinned_with_hash, "still listed as unpinned"),
    ("unpinned-nothing", unpinned_nothing, "'hwinfo/3.0' is listed as unpinned but is not a version directory"),
    ("hook-without-note", hook_without_note, "has no 'Why no service"),
    ("too-many-hooks", too_many_hooks, f"{MAX_HOOK_FILES + 1} hook files remain"),
    ("link", link_in_tracker, "is a link or junction"),
    ("registry-file-format", bad_registry_file, "_registry.toml must say format = 2"),
    ("registry-file-missing", no_registry_file, "_registry.toml is missing"),
]


@pytest.mark.parametrize(("damage", "expected"), [pytest.param(d, e, id=name) for name, d, e in RULES])
def test_each_rule_finds_what_it_is_meant_to_find(registry: Path, damage: Callable[[Path], None], expected: str) -> None:
    """After exactly one kind of damage the report is not ok and a problem names it."""
    assert problems_of(registry) == []

    damage(registry)
    found = problems_of(registry)

    assert any(expected in problem for problem in found), found


def test_a_version_listed_as_unpinned_is_accepted_while_it_has_no_hash(registry: Path) -> None:
    """The legacy marker works as designed: no hash and listed is fine; no hash and not listed is a problem."""
    drop_sha(registry)
    assert any("has no sha256" in p for p in problems_of(registry))

    settings = read_registry_file(registry)
    settings["unpinned"] = [*settings["unpinned"], "hwinfo/10.02"]
    write_registry_file(registry, settings)

    assert problems_of(registry) == []


class _Entry:
    """A directory entry that is a version directory with a manifest (a stand-in: Windows cannot hold two case-variant names)."""

    def __init__(self, name: str) -> None:
        """Keep the entry's name."""
        self.name = name

    def is_dir(self) -> bool:
        """Report a directory."""
        return True

    def __truediv__(self, other: str) -> "_Entry":
        """Return the same entry for any child, so ``entry / "pkg.toml"`` also exists."""
        return self

    def is_file(self) -> bool:
        """Report that the manifest exists."""
        return True


class _Package:
    """A package folder listing the given entry names."""

    def __init__(self, *names: str) -> None:
        """Keep the entry names."""
        self.names = names

    def iterdir(self) -> list[_Entry]:
        """Return the entries."""
        return [_Entry(name) for name in self.names]


def test_two_versions_that_differ_only_in_letter_case_are_reported() -> None:
    """A registry cannot be checked out on Windows when two versions differ only in case; the rule names both.

    A real pair cannot be created on a case-insensitive file system, so the folder is a stand-in listing both names.
    """
    found = _stray_problems("tool", _Package("1.0-RC1", "1.0-rc1", "2.0"))

    assert found == ["tool/1.0-rc1: differs only in letter case from tool/1.0-RC1"]


def test_the_hook_limit_is_exactly_thirteen(registry: Path) -> None:
    """The limit is the literal 13 of the issue: thirteen documented hook files pass, a fourteenth fails."""
    assert MAX_HOOK_FILES == 13
    exactly_the_allowed_hooks(registry)
    assert problems_of(registry) == []

    (registry / "gh" / "_check_updates" / "pkg.local" / "h13.py").write_text(
        '"""Why no service: test."""\nPKG_MODULE_API = 2\nVALUE = 1\n', encoding="utf-8", newline="\n"
    )

    assert any("14 hook files remain" in p for p in problems_of(registry))
