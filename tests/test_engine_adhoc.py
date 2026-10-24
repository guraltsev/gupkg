"""Cover the ad-hoc list of ``state.toml`` on purge: only the package's own root's state is touched (decision D19).

Behaviour covered: purging a system package in an elevated run never reads or writes the user's ``state.toml`` (garbage or
valid, byte-identical afterwards); an unreadable state file never turns a completed purge into a failure (it is a
warning); purging an ad-hoc package removes exactly its own entry from the state of its own root and leaves the other
entries; the entry is removed before the folder is deleted.

Real: the engine and real folders. Mocked: ``FakeSystem`` (elevated).
Out of scope: registering an ad-hoc root (test_engine_distribution).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gupkg.catalog.local import read_adhoc_roots
from gupkg.engine.executor import _forget_adhoc
from gupkg.tomlio import read_toml, write_toml
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

SHIM = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'


def system_package(tmp_path: Path) -> World:
    """An elevated machine with tool installed in the system root."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system", tables=SHIM)
    assert world.one("install", "system:tool").ok
    world.engine.confirm = lambda question: True
    return world


def test_an_elevated_purge_ignores_a_garbage_user_state_file(tmp_path: Path) -> None:
    """The purge completes and the user's unreadable state file is not read, changed or reported."""
    world = system_package(tmp_path)
    state = world.user / ".gupkg" / "state.toml"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_bytes(b"adhoc = [oops")

    result = world.one("uninstall", "system:tool", purge=True)

    assert result.ok and result.status == "purged" and not (world.system_root / "tool").exists()
    assert state.read_bytes() == b"adhoc = [oops" and result.warnings == ()


def test_an_elevated_purge_never_rewrites_a_valid_user_state_file(tmp_path: Path) -> None:
    """A user's state naming the same path stays byte-identical after an elevated purge of a system package."""
    world = system_package(tmp_path)
    state = world.user / ".gupkg" / "state.toml"
    write_toml(state, {"adhoc": [{"path": str(world.system_root / "tool"), "mode": "user"}]})
    before = state.read_bytes()

    result = world.one("uninstall", "system:tool", purge=True)

    assert result.ok and state.read_bytes() == before


def test_purging_an_adhoc_package_removes_only_its_own_entry_from_its_own_root(tmp_path: Path) -> None:
    """The ad-hoc entry goes from the state of its root; other entries and other roots' files stay as they were."""
    world = World(tmp_path)
    world.engine.confirm = lambda question: True
    world.publish_script("tool", "1.0.0", root=world.base / "downloads", tables=SHIM)
    folder = world.base / "downloads" / "tool" / "1.0.0"
    assert world.one("install", str(folder), in_place=True).ok
    package = folder.parent
    state = world.user / ".gupkg" / "state.toml"
    other = {"path": str(tmp_path / "elsewhere" / "other"), "mode": "user"}
    adhoc, _ = read_adhoc_roots(world.user / ".gupkg")
    write_toml(state, {"adhoc": [other, {"path": str(package), "mode": "user"}]})
    system_state = world.system_root / ".gupkg" / "state.toml"
    write_toml(system_state, {"adhoc": [{"path": str(tmp_path / "elsewhere" / "sys"), "mode": "system"}]})
    system_before = system_state.read_bytes()

    result = world.one("uninstall", str(package), purge=True)

    roots, problem = read_adhoc_roots(world.user / ".gupkg")
    assert result.ok and not package.exists() and problem is None and [Path(item.path) for item in roots] == [Path(other["path"])]
    assert system_state.read_bytes() == system_before and adhoc


def test_an_unreadable_state_file_is_a_warning_not_a_failed_purge(tmp_path: Path) -> None:
    """The ad-hoc package's own state is garbage: the purge still completes and says what it could not do."""
    world = World(tmp_path)
    world.engine.confirm = lambda question: True
    world.publish_script("tool", "1.0.0", root=world.base / "downloads", tables=SHIM)
    folder = world.base / "downloads" / "tool" / "1.0.0"
    assert world.one("install", str(folder), in_place=True).ok
    package = folder.parent
    state = world.user / ".gupkg" / "state.toml"
    state.write_bytes(b"adhoc = [oops")

    result = world.one("uninstall", str(package), purge=True)

    assert result.ok and result.status == "purged" and not package.exists()
    assert any("could not be removed" in text for text in result.warnings) and state.read_bytes() == b"adhoc = [oops"


@pytest.mark.parametrize("mode", ["user", "portable"])
def test_the_guard_never_lets_an_elevated_run_touch_a_user_or_portable_state_file(tmp_path: Path, mode: str) -> None:
    """`_forget_adhoc` itself, with an elevated engine and a non-system mode (a path the lifecycle does not normally reach): fail closed."""
    world = World(tmp_path, elevated=True)
    package = tmp_path / "elsewhere" / "tool"
    state = world.root(mode) / ".gupkg" / "state.toml"
    write_toml(state, {"adhoc": [{"path": str(package), "mode": mode}]})
    before = state.read_bytes()
    run = SimpleNamespace(adhoc=True, context=SimpleNamespace(mode=mode), settings=world.engine.load(system_mode=True).settings, warnings=[])

    _forget_adhoc(world.engine, run, package)

    assert state.read_bytes() == before and run.warnings == []


def test_the_same_call_removes_the_entry_in_a_normal_run_and_in_the_system_root(tmp_path: Path) -> None:
    """The guard is the only difference: a normal run forgets a user entry, an elevated run forgets a system entry."""
    for elevated, mode in ((False, "user"), (True, "system")):
        world = World(tmp_path / mode, elevated=elevated)
        package = tmp_path / "elsewhere" / mode / "tool"
        state = world.root(mode) / ".gupkg" / "state.toml"
        write_toml(state, {"adhoc": [{"path": str(package), "mode": mode}]})
        run = SimpleNamespace(adhoc=True, context=SimpleNamespace(mode=mode), settings=world.engine.load(system_mode=elevated).settings, warnings=[])

        _forget_adhoc(world.engine, run, package)

        assert "adhoc" not in read_toml(state)
