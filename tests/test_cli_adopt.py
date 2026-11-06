"""Cover ``gupkg pkg adopt``: a program already unpacked in ``App`` becomes a package from the shortcuts its installer made.

Behaviour covered: the links in the ``shortcuts`` folder, or without it the Start Menu links into ``App``, become the Start
Menu shortcuts of the installed package (paths inside ``App``, ``current\\App`` or another version's ``App`` follow the
package, uninstallers and links outside ``App`` are skipped and reported); the install happens where
the version directory stands, with the root taken from the folder's position; an existing ``pkg.toml`` is never replaced without
``--force`` (and then kept as ``pkg.toml.old``); a run whose root is not the folder's root, or with no usable link, changes
nothing; ``--no-install`` writes only the file and the launchers (``install.cmd`` and ``manage.cmd``, those the folder lacks).

Real: the argument parser, the engine, the catalog, real folders. Mocked: ``FakeSystem`` (the Start Menu is a record, so each
``.lnk`` here is an empty placeholder file next to the fake's record of its fields).
Out of scope: reading real ``.lnk`` files (``test_shell_link.py``) and the install steps themselves (``test_cli_lifecycle.py``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gupkg.launchers import launcher_text
from gupkg.system.port import Mode, ShortcutSpec
from gupkg.tomlio import read_toml
from tests.support.cli import CliWorld

pytestmark = pytest.mark.unit


def make_game(world: CliWorld, games: Path, name: str = "Teardown", version: str = "v1.0") -> Path:
    """Return a version directory under *games* whose App holds a game, a launcher and an uninstaller, with their links beside it.

    The links are what an installer's Start Menu entry looked like: the game (with an icon and no working directory), an
    uninstaller, a link to a file outside App, and a link that sits in a sub folder as installers make them.
    """
    # Lay out the unpacked game: a program, an uninstaller and an editor under ``App``.
    folder = games / name / version
    app = folder / "App"
    (app / "bin").mkdir(parents=True)
    for program in ("game.exe", "unins000.exe", "bin\\editor.exe"):
        (app / program).write_bytes(b"MZ")
    # The links an installer leaves: the game, its uninstaller, a link outside ``App`` and a link in a sub folder.
    links = {
        "Game.lnk": ShortcutSpec(str(app / "game.exe"), arguments="-windowed", icon_location=f"{app / 'game.exe'},0", description="The game"),
        "Uninstall Game.lnk": ShortcutSpec(str(app / "unins000.exe"), working_directory=str(app)),
        "Docs\\Website.lnk": ShortcutSpec("C:\\Windows\\explorer.exe"),
        "Docs\\Editor.lnk": ShortcutSpec(str(app / "bin" / "editor.exe"), working_directory=str(app / "bin")),
    }
    # Write each link as a file and as a shortcut record on the fake machine.
    for relative, spec in links.items():
        link = folder / "shortcuts" / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_bytes(b"")
        world.fake.write_shortcut(link, spec)
    return folder


def test_adopt_makes_a_package_of_the_unpacked_program_and_installs_it_in_place(cli_world: CliWorld, tmp_path: Path) -> None:
    """The kept links become Start Menu shortcuts that point into App, uninstallers and outside links are reported as skipped.

    The root is not given: it is two folders above the version directory, so the program is not copied anywhere.
    """
    # Arrange: an unpacked game with its shortcuts folder.
    folder = make_game(cli_world, tmp_path / "Games")

    # Act: adopt the folder, which installs it in place.
    result = cli_world.cli("pkg", "adopt", str(folder))

    # Assert: the manifest lists the usable shortcuts with ``$App``-relative targets.
    assert result.code == 0, result.err
    manifest = read_toml(folder / "pkg.toml")
    rows = {row["name"]: row for row in manifest["shortcut"]}
    assert manifest["format"] == 2 and manifest["source"] == {"shipped": True} and manifest["record_manifest"] is False
    assert set(rows) == {"Game", "Editor"}
    assert rows["Game"] == {
        "name": "Game", "target": "$App\\game.exe", "arguments": "-windowed", "working_dir": "$App",
        "icon_location": "$App\\game.exe,0", "description": "The game",
    }
    assert rows["Editor"]["target"] == "$App\\bin\\editor.exe" and rows["Editor"]["working_dir"] == "$App\\bin"
    # Uninstallers and links outside ``App`` are reported as skipped.
    assert "skipped Uninstall Game.lnk: it is an uninstaller" in result.out
    assert "skipped Docs\\Website.lnk: its target is outside App" in result.out
    # Assert: the installed shortcuts point into the current version.
    installed = {path.name: spec for path, spec in cli_world.fake.shortcuts().items() if path.parent.name == "opt"}
    current = tmp_path / "Games" / "Teardown" / "current" / "App"
    assert {name: spec.target_path for name, spec in installed.items()} == {
        "Game.lnk": str(current / "game.exe"), "Editor.lnk": str(current / "bin" / "editor.exe"),
    }
    assert (tmp_path / "Games" / "Teardown" / "current").exists()
    assert not (cli_world.user / "Teardown").exists()


def test_adopt_never_replaces_a_pkg_toml_without_force_and_keeps_the_old_one_with_it(cli_world: CliWorld, tmp_path: Path) -> None:
    """An existing pkg.toml is refused (exit 2, untouched); --force replaces it and keeps the old text as pkg.toml.old."""
    folder = make_game(cli_world, tmp_path / "Games")
    (folder / "pkg.toml").write_text("name = 'old format'\n", encoding="utf-8")

    refused = cli_world.cli("pkg", "adopt", str(folder), "--no-install")

    assert refused.code == 2 and "--force" in refused.err
    assert (folder / "pkg.toml").read_text(encoding="utf-8") == "name = 'old format'\n"

    replaced = cli_world.cli("pkg", "adopt", str(folder), "--no-install", "--force")

    assert replaced.code == 0, replaced.err
    assert read_toml(folder / "pkg.toml")["format"] == 2
    assert (folder / "pkg.toml.old").read_text(encoding="utf-8") == "name = 'old format'\n"


def test_adopt_changes_nothing_when_the_root_is_elsewhere_or_no_link_is_usable(cli_world: CliWorld, tmp_path: Path) -> None:
    """A --root that is not the folder's root would copy the program, and a folder of only uninstallers has nothing to adopt: both exit 2."""
    # Arrange: an unpacked game.
    folder = make_game(cli_world, tmp_path / "Games")

    # Act: adopt with a root that is not the folder's root.
    elsewhere = cli_world.cli("--root", str(cli_world.user), "pkg", "adopt", str(folder))

    # Assert: the command refuses and writes nothing.
    assert elsewhere.code == 2 and "would copy it" in elsewhere.err
    assert not (folder / "pkg.toml").exists()

    # Arrange: a game whose only remaining link is the uninstaller.
    only_uninstaller = make_game(cli_world, tmp_path / "Other", name="Empty")
    for link in (only_uninstaller / "shortcuts").rglob("*.lnk"):
        if link.name != "Uninstall Game.lnk":
            link.unlink()

    # Act: adopt the folder.
    unusable = cli_world.cli("pkg", "adopt", str(only_uninstaller))

    # Assert: there is no usable shortcut and nothing is written.
    assert unusable.code == 2 and "no usable shortcut" in unusable.err
    assert not (only_uninstaller / "pkg.toml").exists()


def test_adopt_without_a_shortcuts_folder_takes_the_start_menu_links_into_app(cli_world: CliWorld, tmp_path: Path) -> None:
    """Without a `shortcuts` folder the user's and the machine's Start Menu are searched for links into this version's App.

    Links to other programs are ignored without a report, and an uninstaller into App is still reported as skipped. Links
    that start the same program with the same arguments make one shortcut, whatever their names: the installer's links in
    both menus and gupkg's own link in `Start Menu\\opt` (into `current\\App`), the first found winning; the others are
    reported as skipped. When no link points into App the pkg.toml is written without shortcuts, and the output says so.
    """
    # Arrange: a game without a shortcuts folder.
    folder = make_game(cli_world, tmp_path / "Games")
    shutil.rmtree(folder / "shortcuts")
    app = folder / "App"
    menus = [cli_world.fake.scope_folders(mode).shortcut_root.parent / "Programs" for mode in (Mode.USER, Mode.SYSTEM)]
    opt = cli_world.fake.scope_folders(Mode.USER).shortcut_root
    # The links that the installer and gupkg leave in both Start Menu folders, some of them duplicates.
    links = {
        menus[0] / "Teardown" / "Game.lnk": ShortcutSpec(str(app / "game.exe"), description="The game"),
        menus[1] / "Teardown" / "Game.lnk": ShortcutSpec(str(app / "game.exe"), description="The game"),
        menus[1] / "Teardown" / "Uninstall.lnk": ShortcutSpec(str(app / "unins000.exe")),
        menus[1] / "Other.lnk": ShortcutSpec("C:\\Windows\\notepad.exe"),
        opt / "Editor.lnk": ShortcutSpec(str(folder.parent / "current" / "App" / "bin" / "editor.exe")),
        opt / "Teardown.lnk": ShortcutSpec(str(folder.parent / "current" / "App" / "game.exe")),
        menus[1] / "Teardown" / "Editor (bin).lnk": ShortcutSpec(str(app / "bin" / "editor.exe"), description="Edit"),
    }
    # Write each link as a file and as a shortcut record.
    for link, spec in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_bytes(b"")
        cli_world.fake.write_shortcut(link, spec)

    # Act: adopt, reading the Start Menu.
    found = cli_world.cli("pkg", "adopt", str(folder), "--no-install", "--json")

    # Assert: one shortcut per program, found in the right folders.
    assert found.code == 0, found.err
    assert sorted(found.document()["shortcuts"]) == ["Editor", "Teardown"]
    assert found.document()["read_from"] == {"Editor": str(folder.parent / "current" / "App"), "Teardown": str(folder.parent / "current" / "App")}
    # Duplicates and the uninstaller are reported as skipped.
    assert {(row["file"], row["reason"]) for row in found.document()["skipped"]} == {
        (str(menus[0] / "Teardown" / "Game.lnk"), "it starts the same program as shortcut Teardown"),
        (str(menus[1] / "Teardown" / "Editor (bin).lnk"), "it starts the same program as shortcut Editor"),
        (str(menus[1] / "Teardown" / "Game.lnk"), "it starts the same program as shortcut Teardown"),
        (str(menus[1] / "Teardown" / "Uninstall.lnk"), "it is an uninstaller"),
    }
    # The manifest holds one shortcut for each program.
    rows = {row["name"]: row for row in read_toml(folder / "pkg.toml")["shortcut"]}
    assert rows["Teardown"] == {"name": "Teardown", "target": "$App\\game.exe", "working_dir": "$App"}
    assert rows["Editor"]["target"] == "$App\\bin\\editor.exe" and set(rows) == {"Editor", "Teardown"}
    assert "searched the Start Menu" in found.err and "Other.lnk" not in found.err

    for link in links:
        link.unlink()
    empty = cli_world.cli("pkg", "adopt", str(folder), "--no-install", "--force")

    assert empty.code == 0, empty.err
    assert "shortcut" not in read_toml(folder / "pkg.toml")
    assert "no shortcut found" in empty.out


def test_adopt_takes_links_into_current_or_another_version_as_links_into_this_versions_app(cli_world: CliWorld, tmp_path: Path) -> None:
    """A link into `current\\App` or into the App of another version of the same package becomes a shortcut into this App.

    The target, the working directory and the icon all follow, and the output names each link that was read this way.
    A link into another package's App stays outside App.
    """
    # Arrange: a game and links into its current version, an older version and another package.
    folder = make_game(cli_world, tmp_path / "Games")
    package = folder.parent
    (package / "v0.9" / "App").mkdir(parents=True)
    # Links into this package are rewritten to this version's ``App``; a link into another package is skipped.
    links = {
        "Game.lnk": ShortcutSpec(str(package / "current" / "App" / "game.exe"), icon_location=f"{package / 'current' / 'App' / 'game.exe'},0"),
        "Docs\\Editor.lnk": ShortcutSpec(str(package / "v0.9" / "App" / "bin" / "editor.exe"), working_directory=str(package / "v0.9" / "App" / "bin")),
        "Docs\\Website.lnk": ShortcutSpec(str(tmp_path / "Games" / "Other" / "v1" / "App" / "game.exe")),
        "Old\\Teardown.lnk": ShortcutSpec(str(package / "v0.9" / "App" / "game.exe")),
    }
    # Write each link as a file and as a shortcut record.
    for relative, spec in links.items():
        (folder / "shortcuts" / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / "shortcuts" / relative).write_bytes(b"")
        cli_world.fake.write_shortcut(folder / "shortcuts" / relative, spec)

    # Act: adopt the folder.
    result = cli_world.cli("pkg", "adopt", str(folder), "--no-install")

    # Assert: links into other versions of the package are read as this version's ``App``.
    assert result.code == 0, result.err
    rows = {row["name"]: row for row in read_toml(folder / "pkg.toml")["shortcut"]}
    assert rows["Game"]["target"] == "$App\\game.exe" and rows["Game"]["icon_location"] == "$App\\game.exe,0"
    assert rows["Editor"]["target"] == "$App\\bin\\editor.exe" and rows["Editor"]["working_dir"] == "$App\\bin"
    assert f"shortcut Game (its link points into {package / 'current' / 'App'}: read as this version's App)" in result.out
    assert f"shortcut Editor (its link points into {package / 'v0.9' / 'App'}: read as this version's App)" in result.out
    assert "skipped Docs\\Website.lnk: its target is outside App" in result.out
    assert "skipped Old\\Teardown.lnk: it starts the same program as shortcut Game" in result.out and "Teardown" not in rows


def test_adopt_recognises_app_when_the_link_spells_the_folder_differently(cli_world: CliWorld, tmp_path: Path) -> None:
    """A link that names App by its short (8.3) spelling, or the version directory is given that way, is still a link into App.

    Installers store whichever spelling they saw; the same folder must not be judged "outside App" because of it.
    """
    import ctypes

    # Arrange: a link that spells the ``App`` folder with its 8.3 short name.
    folder = make_game(cli_world, tmp_path / "Games")
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(folder / "App"), buffer, 1024)
    if buffer.value.lower() == str(folder / "App").lower():
        pytest.skip("8.3 short names are disabled on this volume")
    short_app = Path(buffer.value)
    link = folder / "shortcuts" / "Game.lnk"
    cli_world.fake.write_shortcut(link, ShortcutSpec(str(short_app / "game.exe"), working_directory=str(short_app)))

    # Act: adopt the folder.
    result = cli_world.cli("pkg", "adopt", str(folder), "--no-install")

    # Assert: the short spelling is recognised as ``App``.
    assert result.code == 0, result.err
    rows = {row["name"]: row for row in read_toml(folder / "pkg.toml")["shortcut"]}
    assert rows["Game"]["target"] == "$App\\game.exe" and rows["Game"]["working_dir"] == "$App"


def test_adopt_puts_no_prefix_on_shortcut_names_unless_the_roots_setting_or_the_option_gives_one(cli_world: CliWorld, tmp_path: Path) -> None:
    """By default a shortcut is named after its link alone; the root's `adopt.shortcut_prefix` adds a folder; `--prefix` beats it.

    The prefix is always relative to the root's Start Menu folder: a full path or a `..` is refused in the option and in the
    setting, and an empty `--prefix` means none even when the setting names one.
    """
    # Arrange: an unpacked game.
    games = tmp_path / "Games"
    folder = make_game(cli_world, games)

    # Adopt with the given options and return the names of the shortcuts written.
    def adopted(*options: str) -> list[str]:
        """Run `adopt --no-install --force` with *options* and return the names of the shortcuts it wrote."""
        result = cli_world.cli("pkg", "adopt", str(folder), "--no-install", "--force", "--json", *options)
        assert result.code == 0, result.err
        return result.document()["shortcuts"]

    # Act and assert: no prefix by default, then the root's setting, then the option, and a non-relative prefix is refused.
    assert adopted() == ["Editor", "Game"]
    assert cli_world.cli("--root", str(games), "settings", "set", "adopt.shortcut_prefix", "Action\\Indie").code == 0
    assert adopted() == ["Action\\Indie\\Editor", "Action\\Indie\\Game"]
    assert adopted("--prefix", "Other") == ["Other\\Editor", "Other\\Game"]
    assert adopted("--prefix", "") == ["Editor", "Game"]
    assert cli_world.cli("pkg", "adopt", str(folder), "--no-install", "--force", "--prefix", "C:\\Menu").code == 2
    assert cli_world.cli("pkg", "adopt", str(folder), "--no-install", "--force", "--prefix", "..\\Menu").code == 2
    assert cli_world.cli("--root", str(games), "settings", "set", "adopt.shortcut_prefix", "\\Menu").code == 2


def test_adopt_installs_the_shortcuts_into_the_start_menu_folder_of_the_root(cli_world: CliWorld, tmp_path: Path) -> None:
    """A root whose own settings name another `integration.start_menu` gets its shortcuts there, not in the default folder."""
    games = tmp_path / "Games"
    folder = make_game(cli_world, games)
    menu = tmp_path / "Start Menu" / "Games"
    assert cli_world.cli("--root", str(games), "settings", "set", "integration.start_menu", str(menu)).code == 0

    result = cli_world.cli("pkg", "adopt", str(folder))

    assert result.code == 0, result.err
    placed = {path for path in cli_world.fake.shortcuts() if path.parent == menu}
    assert {path.name for path in placed} == {"Game.lnk", "Editor.lnk"}


def test_adopt_writes_the_launchers_the_folder_lacks_and_keeps_those_it_has(cli_world: CliWorld, tmp_path: Path) -> None:
    """The adopted version gets install.cmd and manage.cmd from the templates; a launcher already there is left as it is."""
    folder = make_game(cli_world, tmp_path / "Games")
    (folder / "install.cmd").write_bytes(b"@echo mine\r\n")

    result = cli_world.cli("pkg", "adopt", str(folder), "--no-install", "--json")

    assert result.code == 0, result.err
    assert result.document()["launchers"] == ["manage.cmd"]
    assert (folder / "install.cmd").read_bytes() == b"@echo mine\r\n"
    assert (folder / "manage.cmd").read_bytes() == launcher_text("tui").encode("ascii")


def test_adopt_no_install_writes_the_file_and_nothing_else(cli_world: CliWorld, tmp_path: Path) -> None:
    """With --no-install only pkg.toml and the launchers are written: no install record, no shortcut on the machine, a document that lists both groups."""
    folder = make_game(cli_world, tmp_path / "Games")
    before = set(cli_world.fake.shortcuts())

    result = cli_world.cli("pkg", "adopt", str(folder), "--no-install", "--json")

    assert result.code == 0, result.err
    document = result.document()
    assert document["shortcuts"] == ["Editor", "Game"] and len(document["skipped"]) == 2
    assert set(cli_world.fake.shortcuts()) == before
    assert not (folder.parent / ".gupkg").exists() and not (folder.parent / "current").exists()
    assert sorted(item.name for item in folder.iterdir()) == ["App", "install.cmd", "manage.cmd", "pkg.toml", "shortcuts"]
