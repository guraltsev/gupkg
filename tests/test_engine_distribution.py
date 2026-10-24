"""Cover installs that download nothing: distribution folders, registry copy-in and the origin label (design 9.2, 21.4, decisions D29, D40).

Behaviour covered: a distribution folder with no registry fingerprint is installed and labelled *origin not verified*; with a
matching fingerprint it is labelled *verified against registry main*; with a mismatching one the install is refused and the
machine is unchanged; after an install, changing a file of the version directory makes ``check`` report the mismatch; the copy
never overwrites a diverging directory; the Mark of the Web is not copied and the receipt records it; an in-place install is
remembered as an ad-hoc root; a registry-only package is copied in with its tracker; a registry that has only a tracker is
copied and the install stops with a hint.

Real: the engine, the distribution copy, the registry cache layout and real folders. Mocked: ``FakeSystem``.
Out of scope: bundled launchers and the bootstrap (issue 033), publishing fingerprints (issue 031).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gupkg.catalog.local import read_adhoc_roots
from gupkg.engine.fingerprint import version_fingerprint
from gupkg.engine.receipts import read_receipt
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401
from tests.mode_support import tree_snapshot

SHIM = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root and an engine."""
    return World(tmp_path)


def distribution(world: World, version: str = "1.0.0") -> Path:
    """Write a script-populated distribution folder outside every root and return its version folder."""
    world.publish_script("tool", version, root=world.base / "downloads", tables=SHIM)
    return world.base / "downloads" / "tool" / version


def test_a_distribution_without_a_registry_fingerprint_is_labelled_origin_not_verified(world: World) -> None:
    """With no registry entry the install is copied in, works, and carries the `origin not verified` label."""
    folder = distribution(world)

    result = world.one("install", str(folder))

    package = world.user / "tool"
    assert result.ok and (package / "1.0.0" / "App" / "app.exe").is_file() and (package / "current").is_dir()
    assert read_receipt(package, "1.0.0").data["checksum"] == {"decision": "origin-not-verified"}
    assert [note["message"] for note in world.one("check", "tool").details["notes"] if note["kind"] == "unverified"] == ["origin not verified"]
    assert folder.is_dir()


def test_a_distribution_with_a_matching_fingerprint_is_verified_against_the_registry(world: World) -> None:
    """A fingerprint published for that package and version that matches labels the install `verified against registry main`."""
    folder = distribution(world)
    world.registry({"tool": {"versions": {"1.0.0": folder}}}, fingerprints=[("tool", "1.0.0")])

    result = world.one("install", str(folder))

    record = read_receipt(world.user / "tool", "1.0.0").data["checksum"]
    assert result.ok and record == {"decision": "registry-fingerprint", "registry": "main"}
    assert not [note for note in world.one("check", "tool").details["notes"] if note["kind"] == "unverified"]


def test_a_distribution_with_a_mismatching_fingerprint_is_refused_and_nothing_changes(world: World) -> None:
    """A different fingerprint refuses the install like a checksum mismatch, naming the files that differ."""
    folder = distribution(world)
    published = world.base / "published" / "1.0.0"
    shutil.copytree(folder, published)
    (published / "pkg.local" / "populate.cmd").write_text("rem the registry's script\n", encoding="utf-8")
    world.registry({"tool": {"versions": {"1.0.0": published}}}, fingerprints=[("tool", "1.0.0")])
    before = tree_snapshot(world.user / "tool")

    result = world.one("install", str(folder))

    assert not result.ok and result.exit_code == 3 and result.error["code"] == "registry-fingerprint-mismatch"
    assert "populate.cmd" in result.error["message"] and "Nothing was installed" in result.error["message"]
    assert not (world.user / "tool" / "1.0.0").exists() and tree_snapshot(world.user / "tool") == before
    assert world.fake.shortcuts() == {} and world.fake.broadcasts == 0


def test_check_reports_a_changed_file_of_the_version_directory_against_the_registry(world: World) -> None:
    """After a verified install, editing the version directory makes `check` report the fingerprint mismatch and the file."""
    folder = distribution(world)
    world.registry({"tool": {"versions": {"1.0.0": folder}}}, fingerprints=[("tool", "1.0.0")])
    world.one("install", str(folder))
    assert world.one("check", "tool").ok
    (world.user / "tool" / "1.0.0" / "pkg.local" / "populate.cmd").write_text("rem edited\n", encoding="utf-8")

    result = world.one("check", "tool")

    problems = {item["kind"]: item["message"] for item in result.details["problems"]}
    assert not result.ok and "populate.cmd" in problems["registry-fingerprint"]


def test_the_copy_never_overwrites_a_directory_that_differs(world: World) -> None:
    """A different version directory already in the root is left alone and the install is refused."""
    folder = distribution(world)
    existing = world.user / "tool" / "1.0.0"
    shutil.copytree(folder, existing)
    (existing / "pkg.local" / "populate.cmd").write_text("rem local edit\n", encoding="utf-8")
    before = (existing / "pkg.local" / "populate.cmd").read_text()

    result = world.one("install", str(folder))

    assert not result.ok and "differs" in result.error["message"]
    assert (existing / "pkg.local" / "populate.cmd").read_text() == before


@pytest.mark.windows_integration
def test_copies_of_a_marked_distribution_carry_no_mark_and_the_receipt_records_it(world: World) -> None:
    """A source whose files carry the Mark of the Web is installed from unmarked copies; the receipt says the source was marked."""
    folder = distribution(world)
    for item in folder.rglob("*"):
        if item.is_file():
            with open(f"{item}:Zone.Identifier", "w", encoding="ascii") as stream:
                stream.write("[ZoneTransfer]\nZoneId=3\n")

    result = world.one("install", str(folder))

    assert result.ok and read_receipt(world.user / "tool", "1.0.0").data["origin"]["source_marked"] is True
    for item in (world.user / "tool" / "1.0.0").rglob("*"):
        if item.is_file():
            with pytest.raises(OSError):
                open(f"{item}:Zone.Identifier", "rb").close()


def test_an_unmarked_distribution_is_recorded_as_unmarked(world: World) -> None:
    """The receipt records that the source carried no mark."""
    folder = distribution(world)

    world.one("install", str(folder))

    assert read_receipt(world.user / "tool", "1.0.0").data["origin"]["source_marked"] is False


def test_an_in_place_install_is_remembered_as_an_adhoc_root(world: World) -> None:
    """--in-place keeps the folder where it is, installs from it and records it in state.toml so list and check see it."""
    folder = distribution(world)

    result = world.one("install", str(folder), in_place=True)

    package = folder.parent
    assert result.ok and (package / "current").is_dir() and not (world.user / "tool").exists()
    adhoc, problem = read_adhoc_roots(world.user / ".gupkg")
    assert problem is None and [(Path(item.path), item.mode) for item in adhoc] == [(package, "user")]


def test_a_registry_only_package_is_copied_in_with_its_tracker(world: World) -> None:
    """Installing a registry-only name creates the package root, copies the newest pinned version and the tracker, and installs."""
    tracker = world.tracker("tool", root=world.base / "scratch").joinpath("pkg.toml").read_text(encoding="utf-8")
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "old"}, "1.1.0": {"app.exe": "new"}}, "tracker": tracker, "tables": SHIM}})

    result = world.one("install", "tool")

    package = world.user / "tool"
    assert result.ok and result.version == "1.1.0" and (package / "_check_updates" / "pkg.toml").is_file()
    assert not (package / "1.0.0").exists() and (package / "current" / "App" / "app.exe").read_text() == "new"
    assert read_receipt(package, "1.1.0").data["origin"] == {"kind": "registry", "copied_from": "main"}


def test_a_registry_with_only_a_tracker_copies_it_and_stops_with_a_hint(world: World) -> None:
    """Case R with no pinned version: the tracker is copied in and the install says to run check-updates first."""
    tracker = world.tracker("tool", root=world.base / "scratch").joinpath("pkg.toml").read_text(encoding="utf-8")
    world.registry({"tool": {"versions": {}, "tracker": tracker}})
    world.fake.fetch_log.clear()

    result = world.one("install", "tool")

    assert not result.ok and "check-updates tool" in result.error["message"] and world.fake.fetch_log == []
    assert (world.user / "tool" / "_check_updates" / "pkg.toml").is_file() and not (world.user / "tool" / "current").exists()


def test_the_fingerprint_of_a_version_directory_ignores_a_downloaded_app(world: World) -> None:
    """A downloaded App is a product of the version, so populating it does not change the version's fingerprint."""
    world.publish("tool", "1.0.0", tables=SHIM)
    folder = world.user / "tool" / "1.0.0"
    before = version_fingerprint(folder)

    world.one("install", "tool")

    assert version_fingerprint(folder) == before and version_fingerprint(folder, include_app=True) != before
