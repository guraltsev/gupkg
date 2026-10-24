"""Cover the fingerprint of a pinned version directory (design 9.2, decision D29).

Behavior covered: the documented algorithm gives the documented value for a known tree and does
not depend on creation order or timestamps; any change of content (one byte, a line ending), name
(including its letter case), or file set (an added, removed or empty file) changes it; the top-level
``App`` and ``gupkg`` folders are not part of it and nothing else is; links and junctions
are refused instead of followed or skipped; ``verify_directory`` accepts a faithful copy,
refuses a changed one and names the files that differ; and a copy of a version directory of the real
registry snapshot is verified against the fingerprint ``_registry.toml`` publishes (and refused
after one file changes), which is what installing a distribution folder will rely on.
Real: files in ``tmp_path``, ``hashlib``, real junctions through ``FakeSystem``, the committed
registry snapshot, the catalog's registry reader. Out of scope: the install engine that will call
``verify_directory`` (issue 028) and the command line (issue 029).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from gupkg.catalog.registry import build_snapshot
from gupkg.services.fingerprint import (
    FingerprintError,
    differing_files,
    file_digests,
    fingerprint_directory,
    verify_directory,
)
from gupkg.system.fake import FakeSystem
from tests.http_fixtures import FIXTURES

SNAPSHOT = FIXTURES / "registry_100"

TREE = {"pkg.toml": b"format = 2\n", "pkg.local/hook.py": b"x = 1\n", "install.cmd": b"@echo off\r\n", "empty.txt": b""}


def build(root: Path, files: dict[str, bytes]) -> Path:
    """Write *files* below *root* (creating folders) and return it."""
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


def spec_value(files: dict[str, bytes]) -> str:
    """Compute the fingerprint straight from the documented text format, independent of the module."""
    lines = sorted(
        (f"{len(content)} {hashlib.sha256(content).hexdigest()} {name}\n" for name, content in files.items()),
        key=lambda line: line.split(" ", 2)[2].encode("utf-8"),
    )
    return hashlib.sha256(("gupkg-fingerprint-1\n" + "".join(lines)).encode("utf-8")).hexdigest()


def test_the_fingerprint_follows_the_documented_algorithm(tmp_path: Path) -> None:
    """A known tree has the value the documented text format gives, and it is a 64-digit lower-case hash."""
    folder = build(tmp_path / "v", TREE)

    value = fingerprint_directory(folder)

    assert value == spec_value(TREE)
    assert len(value) == 64 and value == value.lower()


def test_the_fingerprint_of_a_known_tree_never_changes(tmp_path: Path) -> None:
    """The literal value of a small tree is pinned, so the algorithm cannot drift unnoticed."""
    folder = build(tmp_path / "v", {"pkg.toml": b"format = 2\n", "a/b.txt": b"b"})

    assert fingerprint_directory(folder) == "942f555384e93c9b01ca24fa395b379a2a180028f44cfee682b0c5167a86d425"


def test_creation_order_and_timestamps_do_not_matter(tmp_path: Path) -> None:
    """The same files created in another order, with other modification times, give the same fingerprint."""
    first = build(tmp_path / "one", TREE)
    second = tmp_path / "two"
    build(second, dict(reversed(list(TREE.items()))))
    for path in second.rglob("*"):
        os.utime(path, (1_000_000, 1_000_000))

    assert fingerprint_directory(first) == fingerprint_directory(second)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda root: (root / "pkg.toml").write_bytes(b"format = 3\n"), id="one-byte"),
        pytest.param(lambda root: (root / "pkg.toml").write_bytes(b"format = 2\r\n"), id="line-ending"),
        pytest.param(lambda root: (root / "empty.txt").unlink(), id="file-removed"),
        pytest.param(lambda root: (root / "extra.txt").write_bytes(b"x"), id="file-added"),
        pytest.param(lambda root: (root / "also-empty.txt").write_bytes(b""), id="empty-file-added"),
        pytest.param(lambda root: (root / "empty.txt").rename(root / "empty2.txt"), id="renamed"),
        pytest.param(lambda root: (root / "empty.txt").rename(root / "Empty.txt"), id="case-renamed"),
        pytest.param(lambda root: shutil.move(root / "install.cmd", root / "pkg.local" / "install.cmd"), id="moved"),
        pytest.param(lambda root: (root / "pkg.local" / "hook.py").write_bytes(b"x = 2\n"), id="hook"),
        pytest.param(lambda root: build(root / "pkg.local", {"App/x.bin": b"x"}), id="app-below-top-level"),
    ],
)
def test_any_change_of_content_name_or_file_set_changes_the_fingerprint(tmp_path: Path, change) -> None:
    """One byte, a line ending, a rename (even of letter case only), an added, empty or removed file all change it."""
    folder = build(tmp_path / "v", TREE)
    before = fingerprint_directory(folder)

    change(folder)

    assert fingerprint_directory(folder) != before


@pytest.mark.parametrize("name", ["App", "app", "APP", "gupkg", "Gupkg"])
def test_the_payload_and_the_bundled_gupkg_are_not_part_of_the_fingerprint(tmp_path: Path, name: str) -> None:
    """Files below the top-level ``App`` and ``gupkg`` folders (any letter case) leave the fingerprint unchanged."""
    folder = build(tmp_path / "v", TREE)
    before = fingerprint_directory(folder)

    build(folder / name, {"payload.exe": b"MZ", "sub/lib.dll": b"dll"})

    assert fingerprint_directory(folder) == before
    assert not any(path.casefold().startswith(("app/", "gupkg/")) for path in file_digests(folder))


def test_a_dot_folder_is_covered(tmp_path: Path) -> None:
    """Only ``App`` and ``gupkg`` are skipped: a hidden folder changes the fingerprint like any other file."""
    folder = build(tmp_path / "v", TREE)
    before = fingerprint_directory(folder)

    build(folder / ".gupkg", {"state.toml": b"x"})

    assert fingerprint_directory(folder) != before


def test_a_junction_is_refused_not_followed(tmp_path: Path, system: FakeSystem) -> None:
    """A version directory holding a junction cannot be fingerprinted: the error names it."""
    folder = build(tmp_path / "v", TREE)
    outside = build(tmp_path / "outside", {"secret.txt": b"s"})
    system.create_junction(folder / "link", outside)

    with pytest.raises(FingerprintError, match="link"):
        fingerprint_directory(folder)
    assert verify_directory(folder, "0" * 64)[0].startswith("cannot compute the fingerprint")


def test_a_junction_as_the_directory_itself_is_refused(tmp_path: Path, system: FakeSystem) -> None:
    """The folder to fingerprint must not itself be a link."""
    real = build(tmp_path / "real", TREE)
    system.create_junction(tmp_path / "alias", real)

    with pytest.raises(FingerprintError):
        fingerprint_directory(tmp_path / "alias")


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    """A folder that does not exist is an error, never the fingerprint of nothing."""
    with pytest.raises(FingerprintError):
        fingerprint_directory(tmp_path / "nothing")


def test_verify_accepts_a_faithful_copy_and_names_what_differs(tmp_path: Path) -> None:
    """A byte-for-byte copy verifies; a copy with one changed and one extra file is refused with both named."""
    original = build(tmp_path / "v", TREE)
    published = fingerprint_directory(original)
    copy = tmp_path / "copy"
    shutil.copytree(original, copy)

    assert verify_directory(copy, published, reference=original) == []

    (copy / "pkg.toml").write_bytes(b"format = 9\n")
    (copy / "evil.cmd").write_bytes(b"calc")
    problems = verify_directory(copy, published, reference=original)

    assert problems[0].startswith("the fingerprint is")
    assert "differs: evil.cmd (extra)" in problems and "differs: pkg.toml (changed)" in problems


@pytest.mark.parametrize("expected", ["", "abc", "G" * 64, "A" * 64, "0" * 63])
def test_verify_refuses_a_malformed_published_fingerprint(tmp_path: Path, expected: str) -> None:
    """A published value that is not 64 lower-case hex digits is a problem, not a match."""
    folder = build(tmp_path / "v", TREE)

    assert verify_directory(folder, expected)


def test_differing_files_lists_changed_missing_and_extra() -> None:
    """The comparison of two digest maps reports each kind once, in name order."""
    assert differing_files({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "9", "d": "4"}) == [
        "b (changed)", "c (missing)", "d (extra)",
    ]


def test_a_distribution_copy_of_a_registry_version_is_verified_against_the_published_fingerprint(tmp_path: Path) -> None:
    """The real registry's qbittorrent version matches its published fingerprint, and one changed file in a copy is refused."""
    snapshot = build_snapshot("official", _registry_tree(tmp_path), "0" * 40)
    version = snapshot.entry("qbittorrent").versions[0]
    assert version.fingerprint is not None
    copy = tmp_path / "distribution" / "qbittorrent" / version.name
    shutil.copytree(version.path, copy)

    assert verify_directory(copy, version.fingerprint, reference=version.path) == []

    launcher = copy / "install.cmd"
    launcher.write_bytes(launcher.read_bytes() + b"\r\ncalc.exe\r\n")
    problems = verify_directory(copy, version.fingerprint, reference=version.path)

    assert problems and "differs: install.cmd (changed)" in problems


def _registry_tree(tmp_path: Path) -> Path:
    """Lay the snapshot out as the catalog's registry cache expects (``pkgs`` and ``registry.toml``)."""
    tree = tmp_path / "tree"
    shutil.copytree(SNAPSHOT, tree / "pkgs", ignore=shutil.ignore_patterns("_registry.toml"))
    shutil.copyfile(SNAPSHOT / "_registry.toml", tree / "registry.toml")
    return tree


def test_a_tree_with_too_many_files_or_bytes_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostile directory cannot make the check run for ever: past the file or byte limit it is refused, at the limit it is accepted."""
    monkeypatch.setattr("gupkg.services.fingerprint.MAX_FILES", 3)
    folder = build(tmp_path / "v", {"a": b"1", "b": b"2", "c": b"3"})
    assert fingerprint_directory(folder)

    (folder / "d").write_bytes(b"4")
    with pytest.raises(FingerprintError, match="more than 3 files"):
        fingerprint_directory(folder)

    monkeypatch.setattr("gupkg.services.fingerprint.MAX_FILES", 10)
    monkeypatch.setattr("gupkg.services.fingerprint.MAX_TOTAL_BYTES", 4)
    assert fingerprint_directory(folder)
    (folder / "e").write_bytes(b"5")
    with pytest.raises(FingerprintError, match="more than 4 bytes"):
        fingerprint_directory(folder)
