"""Run the case table of design 5.3 through the engine: what ``check-updates``, ``install`` and ``update`` do in every state.

Behaviour covered, per case (registry only, local versions, local tracker, registry plus local linked, opted out, broken,
diverged): the outcome of ``check-updates <name>``, ``install <name>`` and ``update <name>`` as results and as the state of the
package root afterwards. The table is the owner's: install and update never run a tracker, ``check-updates`` never activates,
``update`` never installs a package that is not installed, a registry pinned version is copied in only when chosen and never
overwrites a local directory.

Real: the engine, the catalog, the registry cache layout, real folders and zip archives. Mocked: ``FakeSystem`` (URL fixtures).
Out of scope: the catalog's own decisions in isolation (``test_catalog_cases``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from gupkg.catalog.record import read_install_record, write_install_record
from gupkg.model.state import InstallRecord, Link
from gupkg.system.port import Scope
from tests.engine_support import BASE_URL, World, pin_no_ambient_elevation  # noqa: F401

ENV = '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root."""
    return World(tmp_path)


def current(world: World) -> str | None:
    """Return the version `current` points at, or ``None``."""
    link = world.user / "tool" / "current"
    return Path(os.path.realpath(link)).name if link.exists() else None


def versions(world: World) -> list[str]:
    """Return the version directories of the package, sorted."""
    return sorted(item.name for item in (world.user / "tool").iterdir() if item.is_dir() and item.name[0].isdigit())


def tracker_text(world: World) -> str:
    """Return the text of a tracker for ``tool``."""
    return world.tracker("tool", root=world.base / "scratch").joinpath("pkg.toml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# R: registry only
# ---------------------------------------------------------------------------


def test_case_r_registry_only(world: World) -> None:
    """check-updates and update refuse with hints; install copies the newest pinned version and the tracker, and installs."""
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "a"}, "1.1.0": {"app.exe": "b"}}, "tracker": tracker_text(world), "tables": ENV}})

    checked = world.one("check-updates", "tool")
    updated = world.one("update", "tool")
    installed = world.one("install", "tool")

    assert not checked.ok and "install" in checked.error["message"]
    assert not updated.ok and updated.error["code"] == "not-installed" and "install" in updated.error["message"]
    assert installed.ok and installed.version == "1.1.0" and versions(world) == ["1.1.0"]
    assert (world.user / "tool" / "_check_updates" / "pkg.toml").is_file() and current(world) == "1.1.0"


def test_case_r_registry_with_only_a_tracker(world: World) -> None:
    """The tracker is copied and install stops with `no version known yet; run gupkg check-updates`; check-updates then writes the version."""
    world.registry({"tool": {"versions": {}, "tracker": tracker_text(world)}})

    first = world.one("install", "tool")

    assert not first.ok and first.error["code"] == "no-version-known" and "check-updates tool" in first.error["message"]
    world.announce("tool", "1.0.0")
    checked = world.one("check-updates", "tool")
    assert checked.ok and checked.version == "1.0.0" and current(world) is None
    assert world.one("install", "tool").ok and current(world) == "1.0.0"


# ---------------------------------------------------------------------------
# V: local versions
# ---------------------------------------------------------------------------


def test_case_v_local_versions_not_installed(world: World) -> None:
    """No tracker means nothing to check; install takes the newest valid local version; update refuses."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.publish("tool", "1.1.0", tables=ENV)

    checked = world.one("check-updates", "tool")
    updated = world.one("update", "tool")
    installed = world.one("install", "tool")

    assert checked.ok and checked.status == "no-new-version" and "no tracker" in checked.details["reason"]
    assert updated.error["code"] == "not-installed"
    assert installed.ok and installed.version == "1.1.0"


def test_case_v_named_version_picks_another(world: World) -> None:
    """`name@version` installs that version instead of the newest."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.publish("tool", "1.1.0", tables=ENV)

    result = world.one("install", "tool@1.0.0")

    assert result.ok and result.version == "1.0.0" and current(world) == "1.0.0"


def test_case_v_installed(world: World) -> None:
    """install repairs the active version; update activates the newer local one; a second update says up to date."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV)

    repaired = world.one("install", "tool")
    updated = world.one("update", "tool")
    again = world.one("update", "tool")

    assert repaired.status == "repaired" and repaired.version == "1.0.0"
    assert updated.status == "updated" and updated.version == "1.1.0" and current(world) == "1.1.0"
    assert again.ok and again.status == "up-to-date"


# ---------------------------------------------------------------------------
# T: local tracker
# ---------------------------------------------------------------------------


def test_case_t_tracker_with_versions_not_installed(world: World) -> None:
    """check-updates runs the tracker and activates nothing; install takes the newest local version and never runs the tracker."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.tracker("tool", tables=ENV)
    world.announce("tool", "1.1.0")

    checked = world.one("check-updates", "tool")
    assert checked.ok and checked.version == "1.1.0" and current(world) is None
    world.fake.fetch_log.clear()
    installed = world.one("install", "tool")

    assert installed.ok and installed.version == "1.1.0"
    assert f"{BASE_URL}/tool-latest.json" not in world.fake.fetch_log


def test_case_t_installed_after_a_check(world: World) -> None:
    """The package is outdated once check-updates wrote a newer directory; update activates it; install only repairs."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.tracker("tool", tables=ENV)
    world.one("install", "tool")
    world.announce("tool", "1.1.0")
    world.one("check-updates", "tool")

    repaired = world.one("install", "tool")
    updated = world.one("update", "tool")

    assert repaired.version == "1.0.0" and updated.version == "1.1.0" and current(world) == "1.1.0"


# ---------------------------------------------------------------------------
# R+V/T: linked, opted out, diverged
# ---------------------------------------------------------------------------


def test_case_linked_update_takes_the_newest_of_local_and_registry(world: World) -> None:
    """Update chooses the newest of the local directories and the registry's pinned versions; a registry version is copied in only then."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}, "1.2.0": {"app.exe": "registry"}}, "tables": ENV}})
    world.publish("tool", "1.5.0", tables=ENV)

    result = world.one("update", "tool")

    assert result.ok and result.version == "1.5.0" and versions(world) == ["1.0.0", "1.5.0"]


def test_case_linked_registry_newer_is_copied_in(world: World) -> None:
    """A registry pinned version newer than every local one is copied into the package root and activated."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}, "1.2.0": {"app.exe": "registry"}}, "tables": ENV}})

    result = world.one("update", "tool")

    assert result.version == "1.2.0" and versions(world) == ["1.0.0", "1.2.0"] and current(world) == "1.2.0"


def test_case_opted_out_follows_local_sources_only(world: World) -> None:
    """After `link --unlink` the registry's newer version is ignored: update says up to date."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    package = world.user / "tool"
    record = read_install_record(package).record
    write_install_record(package, InstallRecord(record.mode, Link(unlinked=True), record.effects))
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}, "1.2.0": {"app.exe": "registry"}}, "tables": ENV}})

    result = world.one("update", "tool")

    assert result.ok and result.status == "up-to-date" and versions(world) == ["1.0.0"]


def test_case_diverged_local_directory_is_kept_and_never_overwritten(world: World) -> None:
    """A local directory that differs from the registry's copy of the same name is left alone by install and update."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "tool 1.0.0\n"}}, "tables": ENV + '\n[[env]]\nname = "OTHER"\nvalue = "1"\n'}})
    before = (world.user / "tool" / "1.0.0" / "pkg.toml").read_text()

    result = world.one("install", "tool")

    assert result.ok and (world.user / "tool" / "1.0.0" / "pkg.toml").read_text() == before
    assert "OTHER" not in world.fake.env_values(Scope.USER)


# ---------------------------------------------------------------------------
# Broken
# ---------------------------------------------------------------------------


def test_case_broken_update_refuses_and_install_repairs(world: World) -> None:
    """With the active App deleted, update refuses with a hint; install repopulates it and re-applies integration."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV)
    shutil.rmtree(world.user / "tool" / "1.0.0" / "App")

    refused = world.one("update", "tool")
    repaired = world.one("install", "tool")

    assert not refused.ok and refused.error["code"] == "needs-repair" and "check --fix" in refused.error["message"]
    assert repaired.ok and repaired.status == "repaired" and (world.user / "tool" / "1.0.0" / "App" / "app.exe").is_file()
    assert world.one("update", "tool").version == "1.1.0"


def test_case_broken_dangling_current_is_repaired_by_install(world: World) -> None:
    """A `current` whose target was deleted makes update refuse; install picks the newest valid local version again."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV)
    shutil.rmtree(world.user / "tool" / "1.0.0")

    refused = world.one("update", "tool")
    repaired = world.one("install", "tool")

    assert not refused.ok and repaired.ok and repaired.version == "1.1.0" and current(world) == "1.1.0"
