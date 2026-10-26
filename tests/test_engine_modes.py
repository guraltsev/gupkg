"""Cover the install modes end to end: portable persistence, markers, the per-mode ``prepare`` steps, unsupported modes and ``integrate = "none"``.

Behaviour covered: a portable app keeps its settings and ``Data`` across ``update`` and ``switch`` back and forth (they
live in ``<package>\\data`` and are linked into every ``App``); the first population migrates the vendor's defaults into ``data``;
``[user] prepare`` strips a marker in user mode while portable mode creates the ``[portable] markers`` instead;
``--mode system`` on a package without ``system`` in ``modes`` and portable mode on one without ``portable`` fail before any
download; ``integrate = "none"`` leaves HKCU, the Start Menu and ``PATH`` alone and puts shims in ``<root>\\bin``; portable
mode is refused in an elevated run; uninstall keeps ``data``.

Real: the engine and real folders and junctions. Mocked: ``FakeSystem``. Out of scope: real portable applications (manual smoke).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from gupkg.system.port import Scope
from gupkg.tomlio import dumps
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

TOP = 'modes = ["user", "portable"]\n'
PERSIST = """
[portable]
persist = ["Data", "settings.ini"]
markers = ["portable.marker", { path = "conf/local.xml", content = "<local/>" }]

[user]
prepare = [
    ["remove", "portable", "*/legacy.cfg"],
]
"""
MEMBERS = {
    "app.exe": "app", "settings.ini": "vendor default settings\n", "Data/state.txt": "vendor default state\n",
    "portable": "marker of the vendor\n", "sub/legacy.cfg": "legacy",
}


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a configured portable root."""
    return World(tmp_path)


def publish(world: World, version: str, mode: str = "portable", **options) -> Path:
    """Publish a portable-capable version of ``tool`` with the persistence tables."""
    return world.publish("tool", version, mode=mode, members=MEMBERS, top=TOP, tables=PERSIST, **options)


def link_target(path: Path) -> str:
    """Return the real path of a link, lower-cased."""
    return os.path.normcase(os.path.realpath(path))


def test_portable_state_survives_update_and_switch_back_and_forth(world: World) -> None:
    """Settings and Data written by the app keep their content across update and switch, in both directions."""
    publish(world, "1.0.0")
    assert world.one("install", "portable:tool").ok
    package = world.portable / "tool"
    first = package / "1.0.0" / "App"

    # The vendor defaults moved into data and the App is linked to them.
    assert (package / "data" / "settings.ini").read_text() == "vendor default settings\n"
    assert (package / "data" / "Data" / "state.txt").is_file()
    assert link_target(first / "Data") == os.path.normcase(os.path.realpath(package / "data" / "Data"))

    # The app writes its own state.
    (first / "settings.ini").write_text("user settings\n", encoding="utf-8")
    (first / "Data" / "state.txt").write_text("user state\n", encoding="utf-8")
    (first / "Data" / "extra.txt").write_text("more state\n", encoding="utf-8")

    publish(world, "2.0.0")
    assert world.one("update", "tool", mode="portable").ok
    second = package / "2.0.0" / "App"
    assert (second / "settings.ini").read_text() == "user settings\n"
    assert (second / "Data" / "state.txt").read_text() == "user state\n" and (second / "Data" / "extra.txt").is_file()

    # The new version writes too; switching back carries that state to the old App.
    (second / "settings.ini").write_text("newer settings\n", encoding="utf-8")
    assert world.one("switch", "tool", version="1.0.0", mode="portable").ok
    assert (first / "settings.ini").read_text() == "newer settings\n" and (first / "Data" / "state.txt").read_text() == "user state\n"
    assert world.one("switch", "tool", version="2.0.0", mode="portable").ok
    assert (second / "settings.ini").read_text() == "newer settings\n"


def test_uninstall_keeps_the_portable_data_folder(world: World) -> None:
    """Uninstalling a portable package leaves data\\ (and the version directories); purge deletes it all."""
    publish(world, "1.0.0")
    world.one("install", "portable:tool")
    package = world.portable / "tool"

    world.one("uninstall", "portable:tool")

    assert (package / "data" / "settings.ini").is_file() and not (package / "current").exists()
    world.engine.yes = True
    assert world.one("uninstall", "portable:tool", purge=True).status == "purged" and not package.exists()


def test_user_prepare_removal_in_user_mode_and_markers_in_portable_mode(world: World) -> None:
    """User mode strips the marker the vendor shipped; portable mode creates the declared markers and keeps the vendor files."""
    publish(world, "1.0.0", mode="user")
    assert world.one("install", "user:tool").ok
    user_app = world.user / "tool" / "current" / "App"
    assert not (user_app / "portable").exists() and not (user_app / "sub" / "legacy.cfg").exists()
    assert (user_app / "settings.ini").is_file() and not (user_app / "portable.marker").exists()

    publish(world, "1.0.0", mode="portable")
    assert world.one("install", "portable:tool").ok
    portable_app = world.portable / "tool" / "current" / "App"
    assert (portable_app / "portable").is_file() and (portable_app / "portable.marker").is_file()
    assert (portable_app / "conf" / "local.xml").read_text() == "<local/>"


MODE_STEPS = """
[user]
prepare = [
    ["create", "user.marker", "u"],
    ["remove", "gone.txt"],
]

[portable]
prepare = [
    ["create", "portable.only"],
]
"""


def test_each_mode_runs_only_its_own_prepare_steps(world: World) -> None:
    """`[user] prepare` creates and removes in a user install; `[portable] prepare` runs in a portable one and nothing of the other mode does."""
    members = {**MEMBERS, "gone.txt": "vendor file"}
    world.publish("tool", "1.0.0", mode="user", members=members, top=TOP, tables=MODE_STEPS)
    assert world.one("install", "user:tool").ok
    user_app = world.user / "tool" / "current" / "App"
    assert (user_app / "user.marker").read_text() == "u" and not (user_app / "gone.txt").exists() and not (user_app / "portable.only").exists()

    world.publish("tool", "1.0.0", mode="portable", members=members, top=TOP, tables=MODE_STEPS)
    assert world.one("install", "portable:tool").ok
    portable_app = world.portable / "tool" / "current" / "App"
    assert (portable_app / "portable.only").is_file() and (portable_app / "gone.txt").is_file() and not (portable_app / "user.marker").exists()


def receipt_path(world: World) -> Path:
    """Return the receipt of version 1.0.0 of the user package ``tool``."""
    return world.user / "tool" / "1.0.0" / "App.info"


def pretend_prepared_for_portable(world: World) -> None:
    """Rewrite the receipt as if the App had been prepared for portable mode, and leave a file in it that a rebuild removes."""
    data = tomllib.loads(receipt_path(world).read_text(encoding="utf-8"))
    assert data["app"]["prepared_for"]["mode"] == "user"
    data["app"]["prepared_for"] = {"mode": "portable", "signature": "0" * 64}
    receipt_path(world).write_text(dumps(data), encoding="utf-8")
    (world.user / "tool" / "1.0.0" / "App" / "left-over.txt").write_text("from the other mode", encoding="utf-8")


def test_the_receipt_records_the_mode_the_app_was_prepared_for(world: World) -> None:
    """A package whose steps differ by mode records the mode and a signature of its steps in the receipt."""
    world.publish("tool", "1.0.0", mode="user", members=MEMBERS, top=TOP, tables=MODE_STEPS)
    assert world.one("install", "user:tool").ok

    prepared = tomllib.loads(receipt_path(world).read_text(encoding="utf-8"))["app"]["prepared_for"]

    assert prepared["mode"] == "user" and len(prepared["signature"]) == 64


def test_an_app_prepared_for_another_mode_is_rebuilt_after_a_yes_and_never_patched(world: World) -> None:
    """Installing a populated version whose receipt says it was prepared for another mode asks, then rebuilds the App from the download."""
    world.publish("tool", "1.0.0", mode="user", members=MEMBERS, top=TOP, tables=MODE_STEPS)
    assert world.one("install", "user:tool").ok
    pretend_prepared_for_portable(world)
    asked: list[str] = []
    world.engine = replace(world.engine, confirm=lambda question: asked.append(question) or True)

    result = world.one("install", "user:tool")

    app = world.user / "tool" / "1.0.0" / "App"
    assert result.ok and len(asked) == 1 and "prepared for portable mode" in asked[0] and "Rebuild it for user mode?" in asked[0]
    assert not (app / "left-over.txt").exists() and (app / "user.marker").is_file()
    assert tomllib.loads(receipt_path(world).read_text(encoding="utf-8"))["app"]["prepared_for"]["mode"] == "user"


def test_a_no_leaves_an_app_that_was_prepared_for_another_mode_untouched(world: World) -> None:
    """With nobody to ask (`--no-input`) the install stops with exit code 5 and the App keeps every file."""
    world.publish("tool", "1.0.0", mode="user", members=MEMBERS, top=TOP, tables=MODE_STEPS)
    assert world.one("install", "user:tool").ok
    pretend_prepared_for_portable(world)
    world.engine = replace(world.engine, no_input=True)

    result = world.one("install", "user:tool")

    assert result.exit_code == 5
    assert (world.user / "tool" / "1.0.0" / "App" / "left-over.txt").is_file()


def test_an_app_with_no_record_and_a_package_without_mode_steps_are_left_alone(world: World) -> None:
    """No `prepared_for` in the receipt (an App built before it was recorded) and no per-mode steps at all mean no question."""
    world.publish("tool", "1.0.0", mode="user", members=MEMBERS, top=TOP, tables=MODE_STEPS)
    assert world.one("install", "user:tool").ok
    data = tomllib.loads(receipt_path(world).read_text(encoding="utf-8"))
    del data["app"]["prepared_for"]
    receipt_path(world).write_text(dumps(data), encoding="utf-8")
    asked: list[str] = []
    world.engine = replace(world.engine, confirm=lambda question: asked.append(question) or True)

    assert world.one("install", "user:tool").status == "repaired" and asked == []

    world.publish("plain", "1.0.0", mode="user", members=MEMBERS, top=TOP)
    assert world.one("install", "user:plain").ok and world.one("install", "user:plain").status == "repaired" and asked == []


@pytest.mark.parametrize(
    ("modes", "selector", "word"),
    [
        pytest.param('modes = ["user"]\n', "system:tool", "system", id="system-not-supported"),
        pytest.param('modes = ["user", "system"]\n', "portable:tool", "portable", id="portable-not-supported"),
    ],
)
def test_an_unsupported_mode_fails_before_any_download(world: World, modes: str, selector: str, word: str) -> None:
    """`--mode system` without `system` in modes, and portable without `portable`, are refused with nothing fetched."""
    folder = world.publish("tool", "1.0.0", top=modes, mode="user")
    for target in (world.system_root, world.portable):
        (target / "tool").mkdir(parents=True, exist_ok=True)
    world.fake.fetch_log.clear()

    mode = selector.split(":")[0]
    (world.root(mode) / "tool" / "1.0.0").mkdir(parents=True, exist_ok=True)
    (world.root(mode) / "tool" / "1.0.0" / "pkg.toml").write_text((folder / "pkg.toml").read_text(), encoding="utf-8")
    result = world.one("install", selector)

    assert not result.ok and result.exit_code == 2 and word in result.error["message"] and world.fake.fetch_log == []


def test_integrate_none_touches_no_registry_start_menu_or_path(world: World) -> None:
    """A portable root with integrate = "none": shims go to <root>\\bin and nothing else of the machine changes."""
    world.machine.write_app_settings(tables="[integration.portable]\nintegrate = 'none'\n")
    tables = (
        '[[shortcut]]\nname = "Tool"\ntarget = "$App\\\\app.exe"\n\n[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n\n'
        '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n\n[[path]]\nvalue = "$App"\n'
    )
    world.publish("tool", "1.0.0", mode="portable", top=TOP, tables=tables)
    world.fake.registry_log.clear()

    result = world.one("install", "portable:tool")

    assert result.ok, result.error
    assert (world.portable / "bin" / "tool.exe").is_file() and (world.portable / "bin" / "tool.config.toml").is_file()
    assert world.fake.registry_log == [] and world.fake.broadcasts == 0 and world.fake.shortcuts() == {}
    assert not (world.fake.scope_folders(Scope.USER).shortcut_root).exists()


def test_portable_mode_is_refused_in_an_elevated_run(tmp_path: Path) -> None:
    """An elevated gupkg never acts on a portable root (decision D31): the request is refused and nothing is written."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="portable", top=TOP)

    result = world.one("install", "portable:tool")

    assert not result.ok and result.exit_code == 2 and "elevated" in result.error["message"]
    assert not (world.portable / "tool" / "current").exists()


def test_the_package_follows_its_install_record_mode_and_integration_scope(world: World) -> None:
    """A portable package integrates with the user environment by default: shortcuts, shims and PATH land in the user profile."""
    tables = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'
    world.publish("tool", "1.0.0", mode="portable", top=TOP, tables=tables)

    result = world.one("install", "portable:tool")

    assert result.ok and result.mode == "portable"
    shim_folder = world.fake.scope_folders(Scope.USER).bin_dir
    assert world.portable / "bin" != shim_folder
    assert (world.portable / "bin" / "tool.exe").is_file() and str(world.portable / "bin") in world.fake.get_path(Scope.USER)
