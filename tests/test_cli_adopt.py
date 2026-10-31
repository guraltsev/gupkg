"""Cover ``gupkg pkg adopt``: a program already unpacked in ``App`` becomes a package from the shortcuts its installer made.

Behaviour covered: the links in the ``shortcuts`` folder become the Start Menu shortcuts of the installed package (paths
inside ``App`` follow the package, uninstallers and links outside ``App`` are skipped and reported); the install happens where
the version directory stands, with the root taken from the folder's position; an existing ``pkg.toml`` is never replaced without
``--force`` (and then kept as ``pkg.toml.old``); a run whose root is not the folder's root, or with no usable link, changes
nothing; ``--no-install`` writes only the file.

Real: the argument parser, the engine, the catalog, real folders. Mocked: ``FakeSystem`` (the Start Menu is a record, so each
``.lnk`` here is an empty placeholder file next to the fake's record of its fields).
Out of scope: reading real ``.lnk`` files (``test_shell_link.py``) and the install steps themselves (``test_cli_lifecycle.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.system.port import ShortcutSpec
from gupkg.tomlio import read_toml
from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A fake machine with a user root, a system root and a stand-in installed system gupkg."""
    return CliWorld(tmp_path)


def make_game(world: CliWorld, games: Path, name: str = "Teardown", version: str = "v1.0") -> Path:
    """Return a version directory under *games* whose App holds a game, a launcher and an uninstaller, with their links beside it.

    The links are what an installer's Start Menu entry looked like: the game (with an icon and no working directory), an
    uninstaller, a link to a file outside App, and a link that sits in a sub folder as installers make them.
    """
    folder = games / name / version
    app = folder / "App"
    (app / "bin").mkdir(parents=True)
    for program in ("game.exe", "unins000.exe", "bin\\editor.exe"):
        (app / program).write_bytes(b"MZ")
    links = {
        "Game.lnk": ShortcutSpec(str(app / "game.exe"), arguments="-windowed", icon_location=f"{app / 'game.exe'},0", description="The game"),
        "Uninstall Game.lnk": ShortcutSpec(str(app / "unins000.exe"), working_directory=str(app)),
        "Docs\\Website.lnk": ShortcutSpec("C:\\Windows\\explorer.exe"),
        "Docs\\Editor.lnk": ShortcutSpec(str(app / "bin" / "editor.exe"), working_directory=str(app / "bin")),
    }
    for relative, spec in links.items():
        link = folder / "shortcuts" / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_bytes(b"")
        world.fake.write_shortcut(link, spec)
    return folder


def test_adopt_makes_a_package_of_the_unpacked_program_and_installs_it_in_place(world: CliWorld, tmp_path: Path) -> None:
    """The kept links become Start Menu shortcuts that point into App, uninstallers and outside links are reported as skipped.

    The root is not given: it is two folders above the version directory, so the program is not copied anywhere.
    """
    folder = make_game(world, tmp_path / "Games")

    result = world.cli("pkg", "adopt", str(folder))

    assert result.code == 0, result.err
    manifest = read_toml(folder / "pkg.toml")
    rows = {row["name"]: row for row in manifest["shortcut"]}
    assert manifest["format"] == 2 and manifest["source"] == {"shipped": True} and manifest["record_manifest"] is False
    assert set(rows) == {"Games\\Game", "Games\\Editor"}
    assert rows["Games\\Game"] == {
        "name": "Games\\Game", "target": "$App\\game.exe", "arguments": "-windowed", "working_dir": "$App",
        "icon_location": "$App\\game.exe,0", "description": "The game",
    }
    assert rows["Games\\Editor"]["target"] == "$App\\bin\\editor.exe" and rows["Games\\Editor"]["working_dir"] == "$App\\bin"
    assert "skipped Uninstall Game.lnk: it is an uninstaller" in result.out
    assert "skipped Docs\\Website.lnk: its target is outside App" in result.out
    installed = {path.name: spec for path, spec in world.fake.shortcuts().items() if path.parent.name == "Games"}
    current = tmp_path / "Games" / "Teardown" / "current" / "App"
    assert {name: spec.target_path for name, spec in installed.items()} == {
        "Game.lnk": str(current / "game.exe"), "Editor.lnk": str(current / "bin" / "editor.exe"),
    }
    assert (tmp_path / "Games" / "Teardown" / "current").exists()
    assert not (world.user / "Teardown").exists()


def test_adopt_never_replaces_a_pkg_toml_without_force_and_keeps_the_old_one_with_it(world: CliWorld, tmp_path: Path) -> None:
    """An existing pkg.toml is refused (exit 2, untouched); --force replaces it and keeps the old text as pkg.toml.old."""
    folder = make_game(world, tmp_path / "Games")
    (folder / "pkg.toml").write_text("name = 'old format'\n", encoding="utf-8")

    refused = world.cli("pkg", "adopt", str(folder), "--no-install")

    assert refused.code == 2 and "--force" in refused.err
    assert (folder / "pkg.toml").read_text(encoding="utf-8") == "name = 'old format'\n"

    replaced = world.cli("pkg", "adopt", str(folder), "--no-install", "--force")

    assert replaced.code == 0, replaced.err
    assert read_toml(folder / "pkg.toml")["format"] == 2
    assert (folder / "pkg.toml.old").read_text(encoding="utf-8") == "name = 'old format'\n"


def test_adopt_changes_nothing_when_the_root_is_elsewhere_or_no_link_is_usable(world: CliWorld, tmp_path: Path) -> None:
    """A --root that is not the folder's root would copy the program, and a folder of only uninstallers has nothing to adopt: both exit 2."""
    folder = make_game(world, tmp_path / "Games")

    elsewhere = world.cli("--root", str(world.user), "pkg", "adopt", str(folder))

    assert elsewhere.code == 2 and "would copy it" in elsewhere.err
    assert not (folder / "pkg.toml").exists()

    only_uninstaller = make_game(world, tmp_path / "Other", name="Empty")
    for link in (only_uninstaller / "shortcuts").rglob("*.lnk"):
        if link.name != "Uninstall Game.lnk":
            link.unlink()

    unusable = world.cli("pkg", "adopt", str(only_uninstaller))

    assert unusable.code == 2 and "no usable shortcut" in unusable.err
    assert not (only_uninstaller / "pkg.toml").exists()


def test_adopt_recognises_app_when_the_link_spells_the_folder_differently(world: CliWorld, tmp_path: Path) -> None:
    """A link that names App by its short (8.3) spelling, or the version directory is given that way, is still a link into App.

    Installers store whichever spelling they saw; the same folder must not be judged "outside App" because of it.
    """
    import ctypes

    folder = make_game(world, tmp_path / "Games")
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(folder / "App"), buffer, 1024)
    if buffer.value.lower() == str(folder / "App").lower():
        pytest.skip("8.3 short names are disabled on this volume")
    short_app = Path(buffer.value)
    link = folder / "shortcuts" / "Game.lnk"
    world.fake.write_shortcut(link, ShortcutSpec(str(short_app / "game.exe"), working_directory=str(short_app)))

    result = world.cli("pkg", "adopt", str(folder), "--no-install")

    assert result.code == 0, result.err
    rows = {row["name"]: row for row in read_toml(folder / "pkg.toml")["shortcut"]}
    assert rows["Games\\Game"]["target"] == "$App\\game.exe" and rows["Games\\Game"]["working_dir"] == "$App"


def test_adopt_no_install_writes_the_file_and_nothing_else(world: CliWorld, tmp_path: Path) -> None:
    """With --no-install only pkg.toml is written: no install record, no shortcut on the machine, a document that lists both groups."""
    folder = make_game(world, tmp_path / "Games")
    before = set(world.fake.shortcuts())

    result = world.cli("pkg", "adopt", str(folder), "--no-install", "--json")

    assert result.code == 0, result.err
    document = result.document()
    assert document["shortcuts"] == ["Games\\Editor", "Games\\Game"] and len(document["skipped"]) == 2
    assert set(world.fake.shortcuts()) == before
    assert not (folder.parent / ".gupkg").exists() and not (folder.parent / "current").exists()
