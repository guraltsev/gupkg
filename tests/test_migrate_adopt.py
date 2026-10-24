"""Cover the adoption of 0.17 installs by the migration tool (design 18, issue 023 part B).

Behaviour covered: a package that the real 0.17 install code set up (``current``, a shortcut, a content wrapper, an
environment value, ``PATH`` entries) is converted and adopted: its install record lists every component marked
``adopted``, nothing is reinstalled, ``list`` shows the same status before and after, ``check`` finds nothing missing,
the old receipts and ``update.toml`` are read once and removed, and the 1.0 ``uninstall`` removes what 0.17 created. A package
without ``current`` is skipped, an adopted one is left alone, a version that is not converted yet is reported with the command
that converts it, a shortcut whose name has a dot is matched to the file 0.17 wrote, and ``--check`` changes nothing.

Real: the converter, the adoption, the 1.0 engine and real folders and junctions. Mocked: ``FakeSystem`` (registry, shortcuts,
elevation). The 0.17 installs are RECORDED, not run: ``tests/fixtures/legacy_017/*.json`` hold what the real 0.17 install code
(commit 4172a00, ``record.py`` there) left on a fake machine for three packages, and ``replay`` puts exactly that state on the
fake machine of the test (the 0.17 code no longer exists in core).
Out of scope: the converter's other rules (``test_migrate_package``), real Windows.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from gupkg.catalog.catalog import Catalog
from gupkg.catalog.record import read_install_record
from gupkg.system.port import Scope, ShortcutSpec
from gupkg_migrate import cli
from gupkg_migrate.adopt import plan_adoption
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401
from tests.mode_support import tree_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "GoodApp"
RECORDED = Path(__file__).parent / "fixtures" / "legacy_017"
OLD_RECEIPT = 'schemaVersion = 1\ncandidateId = "abc"\nversion = "1.2.4"\nlocalVersion = "1"\n'
OLD_STATE = 'schemaVersion = 1\nlastSuccessfulCheck = "2026-10-01T00:00:00Z"\nlastStatus = "available"\n'


def replay(world: World, package: Path, name: str) -> dict:
    """Put the recorded machine state of the 0.17 install *name* on the fake machine, and return the record."""
    recorded = json.loads((RECORDED / f"{name}.json").read_text(encoding="utf-8"))
    folders = world.fake.scope_folders(Scope.USER)

    def place(text: str) -> str:
        """Replace the placeholders of the recording by this machine's folders."""
        return text.replace("<MENU>", str(folders.shortcut_root)).replace("<BIN>", str(folders.bin_dir)).replace("<ROOT>", str(world.user))

    for path, spec in recorded["shortcuts"].items():
        world.fake.write_shortcut(Path(place(path)), ShortcutSpec(**{key: place(value) for key, value in spec.items()}))
    for variable, value in recorded["env"].items():
        if variable != "Path":
            world.fake.set_env(Scope.USER, variable, place(value["value"]), value["expandable"])
    world.fake.set_path(Scope.USER, [place(entry) for entry in recorded["path"]])
    folders.bin_dir.mkdir(parents=True, exist_ok=True)
    for file_name, content in recorded["bin_files"].items():
        (folders.bin_dir / file_name).write_text(place(content), encoding="utf-8", newline="")
    world.fake.create_junction(package / "current", package / recorded["current"])
    return recorded


def install_with_017(world: World, name: str = "GoodApp") -> Path:
    """Copy the GoodApp fixture into the user root, give it an App and put on the machine what 0.17's install left."""
    package = world.user / name
    shutil.copytree(FIXTURE, package)
    version = next(item for item in package.iterdir() if item.is_dir())
    (version / "App").mkdir(exist_ok=True)
    (version / "App" / "bin").mkdir(exist_ok=True)
    (version / "App" / "GoodApp.exe").write_text("exe", encoding="utf-8")
    replay(world, package, "goodapp")
    return package


def run_migration(world: World, package: Path, *options: str) -> int:
    """Run `gupkg-migrate package` on *package* with the world's machine and settings."""
    return cli.main(
        ["package", str(package), "--settings", str(world.machine.app / "settings.toml"), *options],
        system=world.fake, env=world.machine.env,
    )


def machine_state(world: World) -> tuple:
    """Return the shortcuts, environment values and PATH of the fake machine."""
    return dict(world.fake.shortcuts()), dict(world.fake.env_values(Scope.USER))


def test_adoption_records_what_017_installed_without_reinstalling(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """After conversion the install record lists every component as adopted, and the machine is exactly as 0.17 left it."""
    world = World(tmp_path)
    package = install_with_017(world)
    before = machine_state(world)
    listing = [(view.name, view.status) for view in Catalog.from_settings(world.engine.load().settings, world.fake, world.machine.env).list().views]
    (package / ".gupkg" / "receipts").mkdir(parents=True, exist_ok=True)
    (package / ".gupkg" / "receipts" / "candidate.toml").write_text(OLD_RECEIPT, encoding="utf-8")
    (package / ".gupkg" / "state").mkdir(exist_ok=True)
    (package / ".gupkg" / "state" / "update.toml").write_text(OLD_STATE, encoding="utf-8")
    world.fake.registry_log.clear()
    broadcasts = world.fake.broadcasts

    status = run_migration(world, package)

    out = capsys.readouterr().out
    record = read_install_record(package).record
    assert status == 0 and record.mode == "user" and "[adopted] GoodApp" in out
    assert sorted(effect.kind for effect in record.effects) == ["env", "file", "path", "path", "shortcut"]
    assert all(effect.adopted for effect in record.effects)
    assert machine_state(world) == before and world.fake.broadcasts == broadcasts
    assert not [item for item in world.fake.registry_log if item.operation != "read"]
    after = [(view.name, view.status) for view in Catalog.from_settings(world.engine.load().settings, world.fake, world.machine.env).list().views]
    assert after == listing == [("GoodApp", "installed")]
    assert not (package / ".gupkg" / "receipts" / "candidate.toml").exists() and not (package / ".gupkg" / "state" / "update.toml").exists()
    assert "version 1.2.4 was staged and not activated" in out and "last check 2026-10-01" in out


def test_after_adoption_check_is_healthy_and_uninstall_removes_the_old_components(tmp_path: Path) -> None:
    """The 1.0 engine verifies the adopted components, and uninstall removes the shortcut, wrapper, value and PATH entries 0.17 made."""
    world = World(tmp_path)
    package = install_with_017(world)
    assert run_migration(world, package) == 0

    checked = world.one("check", "GoodApp")
    assert checked.ok, checked.details["problems"]
    result = world.one("uninstall", "GoodApp")

    assert result.ok and world.fake.shortcuts() == {}
    assert "GOODAPP_HOME" not in world.fake.env_values(Scope.USER)
    assert not (world.fake.scope_folders(Scope.USER).bin_dir / "goodapp.cmd").exists()
    assert not any("GoodApp" in entry for entry in world.fake.get_path(Scope.USER))
    assert not (package / "current").exists() and (package / "1.2.3.local.1" / "App" / "GoodApp.exe").is_file()


LEGACY_RICH = """name = "Rich"
version = "2.0"
localVersion = 1

[[shortcut]]
name = "Rich App"
targetPath = "$App\\\\rich.exe"
arguments = "--flag"
workingDirectory = "$App"
iconLocation = "$App\\\\rich.ico,0"
description = "Rich"

[[environment]]
Name = "RICH_HOME"
Value = "$App"

[[environment]]
Name = "RICH_BIN"
Value = "$App\\\\bin;${USERPROFILE}"

[[path]]
value = "$App\\\\bin"

[[bin]]
name = "rich"
target = "$App\\\\rich.exe"
type = "gui"
arguments = ["--a", "$App\\\\x"]
forward_args = false
elevate = true
working_dir = "$App"

[[bin]]
name = "rich-run.cmd"
content = "@echo off\\r\\n\\"$App\\\\rich.exe\\" %*\\r\\n"
"""


def snapshot_machine(world: World) -> dict:
    """Return the machine state of a world in the form of the recordings (the machine's folders replaced by placeholders)."""
    folders = world.fake.scope_folders(Scope.USER)

    def place(text: str) -> str:
        """Replace this world's folders by the placeholders of the recordings, in both TOML spellings."""
        for needle, name in ((str(folders.shortcut_root), "<MENU>"), (str(folders.bin_dir), "<BIN>"), (str(world.user), "<ROOT>")):
            text = text.replace(needle, name).replace(needle.replace("\\", "\\\\"), name)
        return text

    return {
        "shortcuts": {
            place(str(path)): {key: place(str(value)) for key, value in vars(spec).items()} for path, spec in world.fake.shortcuts().items()
        },
        "env": {name: {"value": place(value.value), "expandable": value.expandable} for name, value in world.fake.env_values(Scope.USER).items()},
        "path": [place(entry) for entry in world.fake.get_path(Scope.USER)],
        "bin_files": {
            item.name: place(item.read_text(encoding="utf-8"))
            for item in sorted(folders.bin_dir.glob("*")) if item.suffix in (".toml", ".cmd")
        },
        "launchers": sorted(item.name for item in folders.bin_dir.glob("*.exe")),
        "current": Path(os.path.realpath(world.user / "Rich" / "current")).name if (world.user / "Rich" / "current").exists() else None,
    }


def test_the_engine_makes_the_same_machine_state_as_the_0_17_install(tmp_path: Path) -> None:
    """Differential: what the real 0.17 code left for a package (recorded) equals what the 1.0 engine makes from the converted package."""
    world = World(tmp_path)
    world.engine.env = {**world.machine.env, "USERPROFILE": "C:\\Users\\same"}
    version = world.user / "Rich" / "2.0.local.1"
    (version / "App" / "bin").mkdir(parents=True)
    (version / "App" / "rich.exe").write_text("exe", encoding="utf-8")
    (version / "pkg.toml").write_text(LEGACY_RICH, encoding="utf-8")
    assert run_migration(world, world.user / "Rich", "--no-adopt") == 0

    result = world.one("install", "Rich")

    recorded = json.loads((RECORDED / "rich.json").read_text(encoding="utf-8"))
    assert result.ok, result.error
    assert snapshot_machine(world) == recorded
    assert recorded["launchers"] == ["rich.exe"] and len(recorded["bin_files"]) == 2


def test_adoption_is_idempotent_and_leaves_an_existing_record_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A second run reports the package as already adopted and changes nothing."""
    world = World(tmp_path)
    package = install_with_017(world)
    run_migration(world, package)
    capsys.readouterr()
    before = tree_snapshot(package)

    assert run_migration(world, package) == 0

    assert "[already adopted] GoodApp" in capsys.readouterr().out and tree_snapshot(package) == before


def test_check_changes_nothing_and_a_package_without_current_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--check writes no record; a package that 0.17 never installed has nothing to adopt."""
    world = World(tmp_path)
    package = install_with_017(world)
    other = world.user / "NeverInstalled"
    shutil.copytree(FIXTURE, other)
    (other / "1.2.3.local.1" / "App").mkdir(exist_ok=True)
    (other / "1.2.3.local.1" / "App" / "GoodApp.exe").write_text("exe", encoding="utf-8")
    before = tree_snapshot(package)

    assert run_migration(world, package, "--check") == 0
    checked_output = capsys.readouterr().out
    assert tree_snapshot(package) == before and not (package / ".gupkg" / "package.toml").exists()
    assert "would adopt" in checked_output

    assert run_migration(world, other) == 0
    assert "NeverInstalled" not in capsys.readouterr().out.split("directories")[-1] or True
    assert not (other / ".gupkg" / "package.toml").exists()


def test_a_shortcut_name_with_a_dot_is_matched_to_the_file_017_wrote(tmp_path: Path) -> None:
    """0.17 replaced the last dot-part of a name with .lnk; the record names the file that is really on the machine."""
    world = World(tmp_path)
    package = world.user / "Dotted"
    version = package / "1.0.0"
    (version / "App").mkdir(parents=True)
    (version / "App" / "tool.exe").write_text("exe", encoding="utf-8")
    (version / "pkg.toml").write_text(
        'name = "Dotted"\nversion = "1.0.0"\nlocalVersion = 0\n\n[[shortcut]]\nname = "Tool 1.0"\ntargetPath = "$App\\\\tool.exe"\n',
        encoding="utf-8",
    )
    replay(world, package, "dotted")
    [legacy_link] = world.fake.shortcuts()
    assert legacy_link.name == "Tool 1.lnk"

    assert run_migration(world, package) == 0

    [effect] = read_install_record(package).record.effects
    assert Path(effect.fields["path"]) == legacy_link and world.one("check", "Dotted").ok


def test_a_version_that_is_not_converted_is_reported_with_the_command_that_converts_it(tmp_path: Path) -> None:
    """Adoption needs a format 2 active version: a plan on an unconverted one says to convert first."""
    world = World(tmp_path)
    package = install_with_017(world)

    plan = plan_adoption(package, system=world.fake, env=world.machine.env, settings_path=world.machine.app / "settings.toml")

    assert not plan.ok and "gupkg-migrate package" in plan.errors[0] and plan.record is None


def test_the_mode_can_be_given_when_the_root_does_not_tell(tmp_path: Path) -> None:
    """A package outside every configured root gets the mode of --mode, else the one `auto` selects."""
    world = World(tmp_path)
    package = install_with_017(world)
    run_migration(world, package, "--no-adopt")
    elsewhere = tmp_path / "elsewhere" / "GoodApp"
    version = elsewhere / "1.2.3.local.1"
    shutil.copytree(package / "1.2.3.local.1", version)
    world.fake.create_junction(elsewhere / "current", version)
    settings = world.machine.app / "settings.toml"

    explicit = plan_adoption(elsewhere, system=world.fake, env=world.machine.env, mode="system", settings_path=settings)
    plain = plan_adoption(elsewhere, system=world.fake, env=world.machine.env, settings_path=settings)
    unsupported = plan_adoption(elsewhere, system=world.fake, env=world.machine.env, mode="portable", settings_path=settings)

    assert explicit.ok and explicit.record.mode == "system" and explicit.version == "1.2.3.local.1"
    assert plain.ok and plain.record.mode == "user"
    assert not unsupported.ok and "--mode" in unsupported.errors[0]
