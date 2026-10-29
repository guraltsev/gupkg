"""Cover bundled mode and the root forwarder in the engine (design 21; decisions D14, D15, D32, D43).

Behaviour covered: installing the `gupkg` package writes the root forwarder (`<root>\\.gupkg\\gupkg.cmd` and
`gupkg-manager.cmd`) that calls the installed gupkg, records it in the install record, reports it as drift when it is
changed or removed, and removes it on uninstall; any other package that declares `[root_forwarder]` is refused; a bundled
install (any other package, installed from a distribution folder) leaves no gupkg package, no forwarder, no gupkg shim and no
gupkg `PATH` entry behind, writes nothing outside its root and the Start Menu, and reads no registry value but `PATH`;
`check-updates` and `update` carry the package's own bundled gupkg forward while old versions keep theirs, and switching back
works; `uninstall --purge` leaves only the root's `.gupkg`; `cleanup --bundled` removes bundled copies only when the root's
forwarder points to an installed gupkg, says why otherwise, never touches gupkg's own copy, and plain `cleanup` never touches
bundles; an `App` built by a custom populate script that copies a folder shipped in the version directory is repopulated after
every `App` is deleted, and the shipped folder is never moved.

Real: the engine and real folders. Mocked: `FakeSystem` (registry, shortcuts, processes).
Out of scope: the launchers themselves (test_package_launcher), the distribution copy (test_engine_distribution) and the
release layout (test_release_bundle).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from gupkg.catalog.record import read_install_record
from gupkg.engine.effects import forwarder_target
from gupkg.launchers import launcher_text
from gupkg.pkgcheck import check_directory
from gupkg.system.port import ProcessResult, Mode
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

FORWARDER = "[root_forwarder]\ntarget = '$PackageRoot\\current\\App'\n"
GUPKG_TABLES = (
    '[[shim]]\nname = "gupkg"\ntarget = "$App\\\\gupkg.cmd"\n\n[[shim]]\nname = "gupkg-manager"\ntarget = "$App\\\\gupkg-manager.cmd"\n\n' + FORWARDER
)
SHIM = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root and an engine."""
    return World(tmp_path)


def install_gupkg(world: World, mode: str = "user") -> Path:
    """Install a `gupkg` package that declares the root forwarder, and return its root data folder."""
    world.publish("gupkg", "1.0.0", mode=mode, tables=GUPKG_TABLES)
    result = world.one("install", "gupkg")
    assert result.ok, result.error
    return world.root(mode) / ".gupkg"


# ---------------------------------------------------------------------------
# The root forwarder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["user", "system"])
def test_installing_gupkg_writes_the_root_forwarder_that_calls_the_installed_gupkg(tmp_path: Path, mode: str) -> None:
    """The install creates `<root>\\.gupkg\\gupkg.cmd` and `gupkg-manager.cmd`; each calls the sibling in `gupkg\\current\\App`."""
    world = World(tmp_path, elevated=mode == "system")

    data = install_gupkg(world, mode)

    for name in ("gupkg.cmd", "gupkg-manager.cmd"):
        text = (data / name).read_bytes().decode("ascii")
        assert f'call "%~dp0..\\gupkg\\current\\App\\{name}" %*' in text and text.endswith("exit /b %ERRORLEVEL%\r\n")
    app = world.root(mode) / "gupkg" / "current" / "App"
    app.mkdir(parents=True, exist_ok=True)
    (app / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")
    assert forwarder_target(data).samefile(app / "gupkg.cmd")


def test_the_forwarder_is_recorded_in_the_install_record_and_removed_by_uninstall(world: World) -> None:
    """The effect is in the install record, uninstall removes both files, and the root data folder itself stays."""
    data = install_gupkg(world)

    kinds = [effect.kind for effect in read_install_record(world.user / "gupkg").record.effects]
    assert kinds.count("forwarder") == 2

    assert world.one("uninstall", "gupkg").ok

    assert not (data / "gupkg.cmd").exists() and not (data / "gupkg-manager.cmd").exists()
    assert data.is_dir()


def test_a_missing_or_changed_forwarder_is_reported_by_check_and_restored_by_repair(world: World) -> None:
    """`check` names a deleted or edited forwarder, and `repair` writes it again."""
    data = install_gupkg(world)
    (data / "gupkg.cmd").write_text("@echo off\r\ncall evil.cmd\r\n", encoding="ascii")
    (data / "gupkg-manager.cmd").unlink()

    drift = world.one("check", "gupkg")

    text = str(drift.details)
    assert "root forwarder missing or changed" in text and "gupkg.cmd" in text
    assert world.one("repair", "gupkg").ok
    assert "evil" not in (data / "gupkg.cmd").read_text(encoding="ascii") and (data / "gupkg-manager.cmd").is_file()


def test_another_package_cannot_declare_a_root_forwarder(world: World) -> None:
    """A package that is not `gupkg` and declares `[root_forwarder]` is refused, and nothing is written into the root's data folder."""
    folder = world.publish("tool", "1.0.0", tables=FORWARDER + SHIM)

    result = world.one("install", "tool")

    assert not result.ok
    assert not (world.user / ".gupkg" / "gupkg.cmd").exists()
    assert "[root_forwarder] is valid only in the gupkg package" in " ".join(check_directory(folder).errors)


def test_a_forwarder_target_outside_the_package_is_refused_by_the_manifest() -> None:
    """The target must be a folder below `$PackageRoot`, so a forwarder can only point into the gupkg package of its root."""
    from gupkg.model.manifest import ManifestError, parse_manifest

    for target in ("C:\\\\elsewhere", "$PackageRoot\\\\..\\\\x", "$PackageRoot", "$App\\\\x"):
        with pytest.raises(ManifestError):
            parse_manifest(f"format = 2\n[root_forwarder]\ntarget = '{target}'\n")
    assert parse_manifest(f"format = 2\n{FORWARDER}").root_forwarder.target == "$PackageRoot\\current\\App"


# ---------------------------------------------------------------------------
# A bundled install leaves no gupkg behind
# ---------------------------------------------------------------------------


def distribution_with_bundle(world: World, version: str = "1.0.0") -> Path:
    """Write a shipped distribution `tool` with a bundled gupkg and its launchers in Downloads, outside every root."""
    folder = world.publish_shipped("tool", version, root=world.base / "downloads", tables=SHIM, hashed=False)
    (folder / "gupkg").mkdir()
    (folder / "gupkg" / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")
    (folder / "install.cmd").write_bytes(launcher_text("install").encode("ascii"))
    (folder / "gupkg-tui.cmd").write_bytes(launcher_text("tui").encode("ascii"))
    return folder


def test_a_bundled_install_leaves_no_gupkg_behind_and_touches_nothing_else(world: World) -> None:
    """Installing a distribution adds the program only: no gupkg package, forwarder, gupkg shim or PATH entry; one PATH read-modify-write."""
    folder = distribution_with_bundle(world)
    world.fake.registry_log.clear()

    result = world.one("install", str(folder))

    assert result.ok, result.error
    assert (world.user / "tool" / "1.0.0" / "gupkg" / "gupkg.cmd").is_file()
    assert sorted(item.name for item in world.user.iterdir() if item.name != "tool") == []
    assert not (world.user / ".gupkg" / "gupkg.cmd").exists() and not (world.user / "gupkg").exists()
    bin_names = {item.name for item in world.mode_context("user").bin_dir.iterdir()}
    assert not any(name.startswith("gupkg") for name in bin_names)
    assert not any("gupkg" in entry.lower() for entry in world.fake.get_path(Mode.USER))
    assert {access.name for access in world.fake.registry_log if access.operation == "read"} <= {"Path"}

    # Nothing is created in the per-user or machine-wide application folders.
    for variable in ("APPDATA", "ProgramData"):
        assert not Path(world.machine.env[variable]).exists()
    assert not (world.base / "home" / "AppData").exists()


def test_a_bundled_install_manages_itself_across_a_check_and_an_update(world: World) -> None:
    """check-updates carries the bundled gupkg into the new version; the old one keeps its own; switching back works."""
    folder = distribution_with_bundle(world)
    tracker = world.tracker("tool", root=folder.parent.parent.parent / "downloads-tracker", tables=SHIM)
    assert world.one("install", str(folder)).ok
    shutil.copytree(tracker, world.user / "tool" / "_check_updates")
    world.announce("tool", "1.1.0")

    assert world.one("check-updates", "tool").ok
    assert world.one("update", "tool").ok

    old, new = world.user / "tool" / "1.0.0", world.user / "tool" / "1.1.0"
    assert (new / "gupkg" / "gupkg.cmd").is_file() and (old / "gupkg" / "gupkg.cmd").is_file()
    assert (new / "install.cmd").read_bytes() == launcher_text("install").encode("ascii")
    assert (world.user / "tool" / "current").resolve() == new.resolve()

    # Switching back to the old version finds its bundled gupkg and launchers untouched.
    assert world.run("switch", "tool", version="1.0.0").results[0].ok
    assert (world.user / "tool" / "current").resolve() == old.resolve() and (old / "gupkg" / "gupkg.cmd").is_file()


def test_purging_a_bundled_install_leaves_only_the_roots_data_folder(world: World) -> None:
    """`uninstall --purge` of a bundled install removes the package; only `.gupkg` (logs) may remain in the root."""
    folder = distribution_with_bundle(world)
    assert world.one("install", str(folder)).ok
    world.engine.confirm = lambda question: True

    result = world.one("uninstall", "tool", purge=True)

    assert result.ok and not (world.user / "tool").exists()
    assert [item.name for item in world.user.iterdir() if item.name != ".gupkg"] == []
    assert [item.name for item in (world.user / ".gupkg").glob("*") if item.name not in ("logs", "state.toml", "registry")] == []


# ---------------------------------------------------------------------------
# cleanup --bundled
# ---------------------------------------------------------------------------


def bundled_install(world: World) -> Path:
    """Install `tool` from a distribution that carries a bundled gupkg; return the installed version folder."""
    assert world.one("install", str(distribution_with_bundle(world))).ok
    return world.user / "tool" / "1.0.0"


def test_cleanup_bundled_keeps_the_copies_and_says_why_when_the_root_has_no_forwarder(world: World) -> None:
    """With no installed gupkg to take over, `cleanup --bundled` removes no bundled copy and reports the reason."""
    version = bundled_install(world)

    result = world.one("cleanup", "tool", bundled=True)

    assert result.ok and (version / "gupkg" / "gupkg.cmd").is_file()
    assert result.details["bundled"] == [] and "no root forwarder" in result.details["bundled_kept"]


def test_cleanup_bundled_removes_the_copies_once_an_installed_gupkg_can_take_over(world: World) -> None:
    """With a root forwarder that points to an installed gupkg the bundled copies go; the launchers stay for the forwarder."""
    version = bundled_install(world)
    data = install_gupkg(world)
    app = world.user / "gupkg" / "current" / "App"
    app.mkdir(parents=True, exist_ok=True)
    (app / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")

    result = world.one("cleanup", "tool", bundled=True)

    assert result.ok and result.details["bundled"] == ["1.0.0"] and not (version / "gupkg").exists()
    assert (version / "install.cmd").is_file() and (data / "gupkg.cmd").is_file()


def test_cleanup_bundled_needs_a_forwarder_that_really_points_to_an_installed_gupkg(world: World) -> None:
    """A forwarder whose gupkg is gone (or that is not one of gupkg's) does not let bundled copies go."""
    version = bundled_install(world)
    data = world.user / ".gupkg"
    data.mkdir(exist_ok=True)
    (data / "gupkg.cmd").write_text("@echo off\r\ncall \"%~dp0..\\gupkg\\current\\App\\gupkg.cmd\" %*\r\n", encoding="ascii")

    result = world.one("cleanup", "tool", bundled=True)

    assert (version / "gupkg" / "gupkg.cmd").is_file() and result.details["bundled"] == []


def test_cleanup_bundled_never_removes_the_bundled_copy_of_gupkg_itself(world: World) -> None:
    """gupkg's own bundled tree is its populate script's input: it stays even with a forwarder."""
    install_gupkg(world)
    own = world.user / "gupkg" / "1.0.0" / "gupkg"
    own.mkdir()
    (own / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")
    app = world.user / "gupkg" / "current" / "App"
    (app / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")

    result = world.one("cleanup", "gupkg", bundled=True)

    assert (own / "gupkg.cmd").is_file() and result.details["bundled"] == []


def test_plain_cleanup_never_removes_a_bundled_copy(world: World) -> None:
    """Without `--bundled`, cleanup leaves the bundled copies alone, even with a forwarder."""
    version = bundled_install(world)
    install_gupkg(world)
    app = world.user / "gupkg" / "current" / "App"
    (app / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")

    assert world.one("cleanup", "tool").ok

    assert (version / "gupkg" / "gupkg.cmd").is_file()


# ---------------------------------------------------------------------------
# Recoverable bundled installs
# ---------------------------------------------------------------------------

def test_a_populate_script_that_copies_a_shipped_folder_makes_app_recoverable(world: World) -> None:
    """After deleting every `App`, `install --refresh` runs the script again and the shipped folder is still where it was."""
    world.publish_script("tool", "1.0.0", script="populate.cmd", tables=SHIM)
    folder = world.user / "tool" / "1.0.0"
    (folder / "payload").mkdir()
    (folder / "payload" / "app.exe").write_text("shipped\n", encoding="utf-8")

    def copy_the_folder(call):
        """Play a populate script that copies `payload` of the version directory into `App` (it never moves it)."""
        context = json.loads(call.input_text)
        shutil.copytree(Path(context["paths"]["version_root"]) / "payload", context["paths"]["app"], dirs_exist_ok=True)
        return ProcessResult(0, "copied\n")

    world.fake.processes["populate.cmd"] = copy_the_folder
    assert world.one("install", "tool").ok
    shutil.rmtree(folder / "App")

    result = world.one("install", "tool", refresh=True)

    assert result.ok and (folder / "App" / "app.exe").read_text(encoding="utf-8") == "shipped\n"
    assert (folder / "payload" / "app.exe").read_text(encoding="utf-8") == "shipped\n"


# ---------------------------------------------------------------------------
# Installing a distribution system-wide
# ---------------------------------------------------------------------------


def test_a_system_install_of_a_distribution_runs_nothing_from_the_source_folder(tmp_path: Path) -> None:
    """From an admin shell the folder in Downloads is read and copied only: the process log has no path under it, and the install uses the admin-only copy."""
    world = World(tmp_path, elevated=True)
    world.publish_script("tool", "1.0.0", script="populate.cmd", tables=SHIM, root=world.base / "downloads")
    source = world.base / "downloads" / "tool" / "1.0.0"

    result = world.one("install", str(source))

    assert result.ok, result.error
    commands = [part for call in (*world.fake.process_log, *world.fake.foreground_log) for part in call]
    assert commands and not [part for part in commands if str(source) in part]
    assert (world.system_root / "tool" / "1.0.0" / "pkg.local" / "populate.cmd").is_file()
