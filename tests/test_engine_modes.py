"""Cover portable packages and the modes a package declares, end to end: persistence, markers, refusals and ``integrate = "none"``.

Behaviour covered: a portable package (``modes = ["portable"]``) installs into the user root like any user package, keeps
its settings and ``Data`` across ``update`` and ``switch`` back and forth (they live in ``<package>\\data`` and are linked
into every ``App``), and the first population migrates the vendor's defaults into ``data``; ``[portable] markers`` are
created for a portable package and for no other; a package whose ``modes`` lack the run's mode (a portable package in
system mode, a system-only package in user mode, a user-only package in system mode) is refused before any file is
created, with the message of the case; ``uninstall`` removes a package whatever its ``modes`` say; ``[integration.user]
integrate = "none"`` leaves HKCU, the Start Menu and ``PATH`` alone and puts shims in ``<root>\\bin``; user mode is refused
in an elevated run; uninstall keeps ``data``.

Real: the engine and real folders and junctions. Mocked: ``FakeSystem``. Out of scope: real portable applications (manual smoke).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gupkg.model.mode import PORTABLE_REFUSAL, SYSTEM_ONLY_REFUSAL, USER_ONLY_REFUSAL
from gupkg.system.port import Scope
from tests.engine_support import World, pin_no_ambient_elevation, pkg_text  # noqa: F401

PORTABLE_TOP = 'modes = ["portable"]\n'
PERSIST = """
[portable]
persist = ["Data", "settings.ini"]
markers = ["portable.marker", { path = "conf/local.xml", content = "<local/>" }]
"""
MEMBERS = {
    "app.exe": "app", "settings.ini": "vendor default settings\n", "Data/state.txt": "vendor default state\n",
    "portable": "marker of the vendor\n", "sub/legacy.cfg": "legacy",
}


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root and a system root."""
    return World(tmp_path)


def publish(world: World, version: str, **options) -> Path:
    """Publish a version of the portable package ``tool`` (with the persistence table) into the user root."""
    return world.publish("tool", version, mode="user", members=MEMBERS, top=PORTABLE_TOP, tables=PERSIST, **options)


def link_target(path: Path) -> str:
    """Return the real path of a link, lower-cased."""
    return os.path.normcase(os.path.realpath(path))


def test_portable_state_survives_update_and_switch_back_and_forth(world: World) -> None:
    """A portable package in the user root keeps what the app wrote in settings and Data across update and switch, in both directions."""
    publish(world, "1.0.0")
    assert world.one("install", "tool").ok
    package = world.user / "tool"
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
    assert world.one("update", "tool").ok
    second = package / "2.0.0" / "App"
    assert (second / "settings.ini").read_text() == "user settings\n"
    assert (second / "Data" / "state.txt").read_text() == "user state\n" and (second / "Data" / "extra.txt").is_file()

    # The new version writes too; switching back carries that state to the old App.
    (second / "settings.ini").write_text("newer settings\n", encoding="utf-8")
    assert world.one("switch", "tool", version="1.0.0").ok
    assert (first / "settings.ini").read_text() == "newer settings\n" and (first / "Data" / "state.txt").read_text() == "user state\n"
    assert world.one("switch", "tool", version="2.0.0").ok
    assert (second / "settings.ini").read_text() == "newer settings\n"


def test_a_portable_package_keeps_its_config_across_an_update_in_user_mode(world: World) -> None:
    """``modes = ["portable"]`` with ``persist = ["config"]``: ``data\\config`` survives an update from 1.0 to 1.1 in the user root."""
    tables = '[portable]\npersist = ["config"]\n'
    members = {"app.exe": "app", "config/settings.ini": "vendor default\n"}
    world.publish("tool", "1.0", mode="user", members=members, top=PORTABLE_TOP, tables=tables)

    first = world.one("install", "tool")

    assert first.ok and first.mode == "user", first.error
    package = world.user / "tool"
    (package / "1.0" / "App" / "config" / "settings.ini").write_text("changed by the user\n", encoding="utf-8")
    world.publish("tool", "1.1", mode="user", members=members, top=PORTABLE_TOP, tables=tables)

    assert world.one("update", "tool").ok

    assert (package / "data" / "config" / "settings.ini").read_text() == "changed by the user\n"
    assert (package / "1.1" / "App" / "config" / "settings.ini").read_text() == "changed by the user\n"
    assert not (world.system_root / "tool").exists()


def test_uninstall_keeps_the_portable_data_folder(world: World) -> None:
    """Uninstalling a portable package leaves data\\ (and the version directories); purge deletes it all."""
    publish(world, "1.0.0")
    world.one("install", "tool")
    package = world.user / "tool"

    world.one("uninstall", "tool")

    assert (package / "data" / "settings.ini").is_file() and not (package / "current").exists()
    world.engine.yes = True
    assert world.one("uninstall", "tool", purge=True).status == "purged" and not package.exists()


def test_markers_are_created_for_a_portable_package_and_for_no_other(world: World) -> None:
    """A portable package gets its declared markers and keeps the vendor files; a package that is not portable gets none."""
    publish(world, "1.0.0")
    assert world.one("install", "tool").ok
    portable_app = world.user / "tool" / "current" / "App"
    assert (portable_app / "portable").is_file() and (portable_app / "portable.marker").is_file()
    assert (portable_app / "conf" / "local.xml").read_text() == "<local/>"

    world.publish("plain", "1.0.0", mode="user", members=MEMBERS)
    assert world.one("install", "plain").ok
    plain_app = world.user / "plain" / "current" / "App"
    assert (plain_app / "settings.ini").is_file() and not (plain_app / "portable.marker").exists()


def publish_in_registry(world: World, base: Path, *, top: str, tables: str = "", mode: str = "user") -> None:
    """Publish ``tool`` 1.0.0 into the registry cache of *mode*'s root, so that nothing of it exists in the root before an install."""
    url, digest = world.serve("tool", "1.0.0", {"app.exe": "app"})
    folder = base / "published" / "1.0.0"
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text(pkg_text(url, sha256=digest, top=top, tables=tables), encoding="utf-8")
    world.registry({"tool": {"versions": {"1.0.0": folder}}}, mode=mode)


def assert_nothing_was_created(world: World, mode: str) -> None:
    """Assert that the package folder, the bin folder, the receipt and the install record of ``tool`` do not exist, and nothing was fetched."""
    package = world.root(mode) / "tool"
    scope = Scope.MACHINE if mode == "system" else Scope.USER
    assert not package.exists(), "the package folder was created"
    assert not (package / "1.0.0" / "App.info").exists(), "a receipt was written"
    assert not world.fake.scope_folders(scope).bin_dir.exists(), "the bin folder was created"
    assert world.fake.fetch_log == [] and world.fake.registry_log == [] and world.fake.shortcuts() == {}


def test_a_portable_package_in_system_mode_is_refused_before_any_file_is_created(tmp_path: Path) -> None:
    """The portable package of the user test, in an elevated (system) run, is refused with the portable message and creates nothing."""
    world = World(tmp_path, elevated=True)
    publish_in_registry(world, tmp_path, top=PORTABLE_TOP, tables='[portable]\npersist = ["config"]\n', mode="system")
    world.fake.fetch_log.clear()

    result = world.one("install", "tool")

    assert not result.ok and result.exit_code == 2 and result.error["message"] == PORTABLE_REFUSAL
    assert PORTABLE_REFUSAL == "This is a portable package: it installs only for a single user. Run gupkg without administrator rights."
    assert_nothing_was_created(world, "system")


def test_a_system_only_package_in_user_mode_is_refused_before_any_file_is_created(world: World, tmp_path: Path) -> None:
    """``modes = ["system"]`` in a normal run: refused with the system-only message that names how to get administrator rights."""
    publish_in_registry(world, tmp_path, top='modes = ["system"]\n')
    world.fake.fetch_log.clear()

    result = world.one("install", "tool")

    assert not result.ok and result.exit_code == 2 and result.error["message"] == SYSTEM_ONLY_REFUSAL
    assert SYSTEM_ONLY_REFUSAL == (
        'This package installs only system-wide. Run gupkg as administrator: use the Start Menu entry "gupkg manager '
        '(administrator)", or run in an administrator shell: gupkg <command> <name>'
    )
    assert_nothing_was_created(world, "user")


def test_a_user_only_package_in_system_mode_is_refused_before_any_file_is_created(tmp_path: Path) -> None:
    """``modes = ["user"]`` in an elevated (system) run: refused with the single-user message."""
    world = World(tmp_path, elevated=True)
    publish_in_registry(world, tmp_path, top='modes = ["user"]\n', mode="system")
    world.fake.fetch_log.clear()

    result = world.one("install", "tool")

    assert not result.ok and result.exit_code == 2 and result.error["message"] == USER_ONLY_REFUSAL
    assert USER_ONLY_REFUSAL == "This package installs only for a single user. Run gupkg without administrator rights."
    assert_nothing_was_created(world, "system")


@pytest.mark.parametrize(
    ("mode", "top", "tables", "elevated"),
    [
        pytest.param("user", 'modes = ["system"]\n', "", False, id="system-only-package-in-user-mode"),
        pytest.param("system", 'modes = ["user"]\n', "", True, id="user-only-package-in-system-mode"),
        pytest.param("system", PORTABLE_TOP, '[portable]\npersist = ["config"]\n', True, id="portable-package-in-system-mode"),
    ],
)
def test_uninstall_is_never_refused_by_the_modes_of_the_package(tmp_path: Path, mode: str, top: str, tables: str, elevated: bool) -> None:
    """A package installed before its ``modes`` narrowed can always be removed: the mode list never refuses ``uninstall``."""
    world = World(tmp_path, elevated=elevated)
    world.publish("tool", "1.0.0", mode=mode, members=MEMBERS)
    assert world.one("install", "tool").ok
    package = world.root(mode) / "tool"
    assert (package / "current").exists()

    # The definition now excludes the run's mode (the same version, rewritten).
    world.publish("tool", "1.0.0", mode=mode, members=MEMBERS, top=top, tables=tables)
    result = world.one("uninstall", "tool")

    assert result.ok, result.error
    assert not (package / "current").exists()


def test_integrate_none_touches_no_registry_start_menu_or_path(world: World) -> None:
    """A user root with integrate = "none": shims go to <root>\\bin and nothing else of the machine changes."""
    world.machine.write_app_settings(tables="[integration.user]\nintegrate = 'none'\n")
    tables = (
        '[[shortcut]]\nname = "Tool"\ntarget = "$App\\\\app.exe"\n\n[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n\n'
        '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n\n[[path]]\nvalue = "$App"\n'
    )
    world.publish("tool", "1.0.0", mode="user", tables=tables)
    world.fake.registry_log.clear()

    result = world.one("install", "tool")

    assert result.ok, result.error
    assert (world.user / "bin" / "tool.exe").is_file() and (world.user / "bin" / "tool.config.toml").is_file()
    assert world.fake.registry_log == [] and world.fake.broadcasts == 0 and world.fake.shortcuts() == {}
    assert not (world.fake.scope_folders(Scope.USER).shortcut_root).exists()
    assert not (world.fake.scope_folders(Scope.USER).bin_dir).exists()


def test_user_mode_is_refused_in_an_elevated_run(tmp_path: Path) -> None:
    """An elevated gupkg never acts on a user root (decision D31): the request is refused and nothing is written."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="user", members=MEMBERS)

    result = world.one("install", "user:tool")

    assert not result.ok and result.exit_code == 2 and "elevated" in result.error["message"]
    assert not (world.user / "tool" / "current").exists()


def test_a_portable_package_integrates_like_any_user_package(world: World) -> None:
    """A portable package gets the normal user integration: its shim lands in the user's bin folder and on the user PATH."""
    tables = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n' + PERSIST
    world.publish("tool", "1.0.0", mode="user", top=PORTABLE_TOP, tables=tables)

    result = world.one("install", "tool")

    assert result.ok and result.mode == "user", result.error
    shim_folder = world.fake.scope_folders(Scope.USER).bin_dir
    assert (shim_folder / "tool.exe").is_file() and str(shim_folder) in world.fake.get_path(Scope.USER)
