"""Cover ``App.info``: the record of how an App was built lives in the version directory.

Behavior covered: an install writes ``<version>\\App.info`` and no file in ``.gupkg\\receipts``; a receipt kept in the old place by gupkg
1.0.0 to 1.0.x is still read and moves to ``App.info`` when it is next written; ``App.info`` is not part of the fingerprint of a
version directory (a registry fingerprint still matches a version that was installed); copying a populated version directory
to another root carries its record, so the mode the App was prepared for travels with it.

Real: the engine, the receipt module, the fingerprint and real folders. Mocked: ``FakeSystem``.
Out of scope: what the receipt holds (``tests/test_engine_store.py``).
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from gupkg.engine.receipts import legacy_receipt_path, list_receipts, read_receipt, receipt_path, update_receipt
from gupkg.services.fingerprint import fingerprint_directory
from tests.engine_support import World


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root."""
    return World(tmp_path)


def test_an_install_writes_app_info_in_the_version_directory_and_nothing_in_the_old_place(world: World) -> None:
    """`<package>\\<version>\\App.info` holds the record; `.gupkg\\receipts` is not created."""
    world.publish("tool", "1.0.0")
    assert world.one("install", "tool").ok
    package = world.user / "tool"

    info = tomllib.loads((package / "1.0.0" / "App.info").read_text(encoding="utf-8"))

    assert info["schema"] == 1 and info["source"]["kind"] == "download" and info["run"]["operation"] == "install"
    assert not (package / ".gupkg" / "receipts").exists() and list_receipts(package) == ["1.0.0"]


def test_an_old_receipt_is_read_and_moves_to_app_info_on_the_next_write(tmp_path: Path) -> None:
    """A receipt in `.gupkg\\receipts` is found when the version has no `App.info`; writing creates `App.info` and removes the old file."""
    package = tmp_path / "tool"
    (package / "1.0.0").mkdir(parents=True)
    old = legacy_receipt_path(package, "1.0.0")
    old.parent.mkdir(parents=True)
    old.write_text('schema = 1\n[source]\nkind = "download"\n', encoding="utf-8")

    assert read_receipt(package, "1.0.0").data["source"]["kind"] == "download" and list_receipts(package) == ["1.0.0"]

    written = update_receipt(package, "1.0.0", {"run": {"operation": "install"}})

    assert written == receipt_path(package, "1.0.0") and not old.exists()
    merged = read_receipt(package, "1.0.0").data
    assert merged["source"]["kind"] == "download" and merged["run"]["operation"] == "install"


def test_app_info_does_not_change_the_fingerprint_of_a_version_directory(tmp_path: Path) -> None:
    """The registry fingerprint covers the files of the version, not the record an install adds to it."""
    version = tmp_path / "tool" / "1.0.0"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text("format = 2\n", encoding="utf-8")
    before = fingerprint_directory(version)

    (version / "App.info").write_text("schema = 1\n", encoding="utf-8")

    assert fingerprint_directory(version) == before


def test_a_populated_version_directory_copied_to_another_root_carries_its_record(world: World) -> None:
    """Copying `<root>\\tool\\1.0.0` (App and App.info) elsewhere keeps what is known about how the App was built."""
    world.publish("tool", "1.0.0")
    assert world.one("install", "tool").ok
    copy = world.base / "elsewhere" / "tool" / "1.0.0"
    shutil.copytree(world.user / "tool" / "1.0.0", copy)

    assert tomllib.loads((copy / "App.info").read_text(encoding="utf-8"))["source"]["kind"] == "download"
