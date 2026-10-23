"""Cover the schema 3 settings file, its layers and the elevated-run filtering.

Behavior covered: every effective value reports the layer it came from; later layers
override earlier ones; the user layer is ignored in elevated and system-mode runs; a
`--settings` file outside the system root is ignored in an elevated run; elevated and
system-mode runs refuse a settings file in a folder that is not admin-only; nothing is
ever read from AppData or ProgramData; invalid files are reported with every problem
and a hint.

Real: ``gupkg.settingsload.load_settings`` and the model, on settings files written
under pytest's temporary folder. Mocked: the administrator-only folder check
(``trust_check``), because the real one reads Windows access-control lists. Out of
scope: the ``gupkg settings show`` command itself and the real folder check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.elevation import UntrustedLocationError
from gupkg.model.settings import SettingsError, default_layer, merge_layers
from gupkg.settingsload import load_settings, settings_file


class World:
    """A fake machine under a temporary folder: roots, an app folder and an environment."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.system = base / "sys"
        self.user = base / "home" / "opt"
        self.portable = base / "home" / "opt-portable"
        self.app = base / "app"
        self.env = {
            "USERPROFILE": str(base / "home"),
            "APPDATA": str(base / "appdata"),
            "LOCALAPPDATA": str(base / "localappdata"),
            "ProgramData": str(base / "programdata"),
        }
        self.reads: list[Path] = []
        self.write(self.app / "settings.toml", self.app_text())

    def app_text(self, extra: str = "") -> str:
        """Return the app layer text: it places the three roots inside the temporary folder."""
        return (
            f"schema = 3\n[roots]\nsystem = '{self.system}'\nuser = '{self.user}'\nportable = '{self.portable}'\n" + extra
        )

    @staticmethod
    def write(path: Path, text: str) -> Path:
        """Write a settings file, creating its folder."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def system_file(self, text: str) -> Path:
        """Write the system root's settings file."""
        return self.write(settings_file(self.system), text)

    def user_file(self, text: str) -> Path:
        """Write the user root's settings file."""
        return self.write(settings_file(self.user), text)

    def read_text(self, path: Path) -> str | None:
        """Return a file's text and remember that it was read."""
        self.reads.append(Path(path))
        try:
            return Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def load(self, *, elevated: bool = False, system_mode: bool = False, settings_path: Path | None = None,
             trust=lambda folder: None):
        """Load settings for a run on this machine."""
        return load_settings(
            env=self.env, elevated=elevated, system_mode=system_mode, app_dir=self.app,
            settings_path=settings_path, read_text=self.read_text, trust_check=trust,
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine whose roots, app folder and AppData live under a temporary folder."""
    return World(tmp_path)


def sources(loaded) -> dict[str, str]:
    """Return {key: layer name} for every effective value."""
    return {row.key: row.layer for row in loaded.settings.effective()}


def test_defaults_alone_give_the_documented_values_and_say_so() -> None:
    """With only the built-in defaults the documented defaults apply and every value is attributed to `defaults`."""
    settings = merge_layers([default_layer()])

    assert settings["default_mode"] == "auto"
    assert settings["roots.system"] == "C:\\opt"
    assert settings["integration.portable.integrate"] == "user"
    assert settings["manager.checksum_missing"] == "ask"
    assert [(r.name, r.source) for r in settings.registries] == [("main", "https://github.com/guraltsev/gupkg-registry.git")]
    assert {row.layer for row in settings.effective()} == {"defaults"}


def test_each_effective_value_reports_the_layer_it_came_from(world: World) -> None:
    """Values set by the app, system, user and command-line layers are attributed to exactly those layers."""
    world.system_file('schema = 3\ndefault_mode = "user"\n[logs]\nkeep_runs = 7\n')
    world.user_file('schema = 3\n[logs]\nkeep_runs = 9\n[ui]\ncolor = "never"\n')
    cli = world.write(world.base / "cli.toml", 'schema = 3\n[cleanup]\nauto = true\n')

    loaded = world.load(settings_path=cli)
    origin = sources(loaded)

    assert loaded.settings["logs.keep_runs"] == 9          # user beats system
    assert (origin["logs.keep_runs"], origin["default_mode"]) == ("user", "system")
    assert (origin["ui.color"], origin["cleanup.auto"]) == ("user", "cli")
    assert origin["roots.system"] == "app"
    assert origin["logs.keep_days"] == "defaults"
    assert [(r.name, r.status) for r in loaded.layers] == [
        ("defaults", "applied"), ("app", "applied"), ("system", "applied"), ("user", "applied"), ("cli", "applied"),
    ]


def test_system_layer_can_move_the_user_root_and_the_user_layer_follows_it(world: World) -> None:
    """The user layer is read from the user root that the system layer names, not from the default one."""
    moved = world.base / "moved" / "opt"
    world.system_file(f"schema = 3\n[roots]\nuser = '{moved}'\n")
    world.user_file('schema = 3\n[ui]\ncolor = "always"\n')
    world.write(settings_file(moved), 'schema = 3\n[ui]\ncolor = "never"\n')

    loaded = world.load()

    assert loaded.settings["ui.color"] == "never"
    assert loaded.settings["roots.user"] == str(moved)


def test_registry_array_is_replaced_by_the_highest_layer_that_sets_it(world: World) -> None:
    """A `[[registry]]` array in a later layer replaces the earlier one entirely."""
    world.user_file('schema = 3\n[[registry]]\nname = "team"\nsource = "https://example.org/team.git"\n')

    loaded = world.load()

    assert [(r.name, r.source) for r in loaded.settings.registries] == [("team", "https://example.org/team.git")]
    assert sources(loaded)["registry"] == "user"


@pytest.mark.parametrize("flags", [{"elevated": True}, {"system_mode": True}], ids=["elevated", "system-mode"])
def test_user_layer_is_ignored_in_elevated_and_system_mode_runs(world: World, flags: dict) -> None:
    """The user layer's values are not applied, the layer is reported as ignored, and its file is never read."""
    world.user_file('schema = 3\n[ui]\ncolor = "never"\n')

    loaded = world.load(**flags)

    assert loaded.settings["ui.color"] == "auto"
    user = next(r for r in loaded.layers if r.name == "user")
    assert user.status == "ignored"
    assert settings_file(world.user) not in world.reads


def test_user_layer_is_applied_in_a_normal_run(world: World) -> None:
    """The same file is applied when the run is neither elevated nor in system mode."""
    world.user_file('schema = 3\n[ui]\ncolor = "never"\n')

    assert world.load().settings["ui.color"] == "never"


def test_settings_file_outside_the_system_root_is_ignored_in_an_elevated_run(world: World) -> None:
    """In an elevated run `--settings` is ignored unless the file is inside the system root."""
    outside = world.write(world.base / "downloads" / "evil.toml", 'schema = 3\n[ui]\ncolor = "never"\n')

    loaded = world.load(elevated=True, settings_path=outside)

    assert loaded.settings["ui.color"] == "auto"
    cli = next(r for r in loaded.layers if r.name == "cli")
    assert cli.status == "ignored"
    assert outside not in world.reads


def test_settings_file_inside_the_system_root_is_used_in_an_elevated_run(world: World) -> None:
    """An elevated run accepts `--settings` from inside the system root (its folder passing the admin-only check)."""
    inside = world.write(world.system / "extra.toml", 'schema = 3\n[ui]\ncolor = "never"\n')

    loaded = world.load(elevated=True, settings_path=inside)

    assert loaded.settings["ui.color"] == "never"
    assert sources(loaded)["ui.color"] == "cli"


def test_settings_file_anywhere_is_used_in_a_normal_run_and_must_exist(world: World) -> None:
    """A normal run takes `--settings` from any folder, and a missing file is an error rather than silence."""
    elsewhere = world.write(world.base / "elsewhere.toml", 'schema = 3\n[ui]\ncolor = "never"\n')

    assert world.load(settings_path=elsewhere).settings["ui.color"] == "never"
    with pytest.raises(SettingsError, match="does not exist"):
        world.load(settings_path=world.base / "absent.toml")


@pytest.mark.parametrize("flags", [{"elevated": True}, {"system_mode": True}], ids=["elevated", "system-mode"])
def test_elevated_and_system_runs_refuse_a_layer_in_a_folder_that_is_not_admin_only(world: World, flags: dict) -> None:
    """A settings file in a folder a standard user can change stops the run instead of being trusted."""
    world.system_file('schema = 3\n[ui]\ncolor = "never"\n')

    def untrusted(folder: Path) -> None:
        raise UntrustedLocationError(f"{folder} is writable by Users")

    with pytest.raises(SettingsError, match="Refusing the"):
        world.load(trust=untrusted, **flags)


def test_normal_run_does_not_need_admin_only_folders(world: World) -> None:
    """A non-elevated user-mode run reads layers from any folder without the admin-only check."""
    world.system_file('schema = 3\n[ui]\ncolor = "never"\n')

    def untrusted(folder: Path) -> None:
        raise UntrustedLocationError("never asked")

    assert world.load(trust=untrusted).settings["ui.color"] == "never"


def test_nothing_is_read_from_appdata_or_programdata(world: World) -> None:
    """Settings files planted in AppData, LocalAppData and ProgramData are never read and never applied."""
    for variable in ("APPDATA", "LOCALAPPDATA", "ProgramData"):
        for relative in ("settings.toml", "gupkg/settings.toml", ".gupkg/settings.toml"):
            world.write(Path(world.env[variable]) / relative, 'schema = 3\n[ui]\ncolor = "never"\n')

    loaded = world.load()

    assert loaded.settings["ui.color"] == "auto"
    forbidden = [Path(world.env[v]) for v in ("APPDATA", "LOCALAPPDATA", "ProgramData")]
    assert not [read for read in world.reads if any(parent in forbidden for parent in read.parents)]


def test_missing_layers_are_skipped_and_reported(world: World) -> None:
    """Absent system and user files are reported as missing and change nothing."""
    loaded = world.load()

    statuses = {r.name: r.status for r in loaded.layers}
    assert statuses["system"] == "missing" and statuses["user"] == "missing"
    assert loaded.settings["roots.system"] == str(world.system)


def test_invalid_layer_lists_every_problem_with_hints(world: World) -> None:
    """An invalid file reports all its problems at once and names the file and the likely key."""
    world.user_file(
        'schema = 3\ndefault_mod = "user"\n[ui]\ncolor = "purple"\n[logs]\nkeep_runs = 0\n[[registry]]\nname = "a"\n'
    )

    with pytest.raises(SettingsError) as caught:
        world.load()

    text = str(caught.value)
    assert len(caught.value.problems) >= 4
    assert "did you mean 'default_mode'" in text
    assert "settings.toml" in text
    assert "'ui.color' must be one of auto, always, never" in text


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("[ui]\ncolor = 'auto'\n", id="no-schema"),
        pytest.param("schema = 2\n", id="schema-2"),
        pytest.param('mode = "manager"\nschema_version = 2\n', id="manager-config"),
    ],
)
def test_files_that_are_not_schema_3_are_refused(world: World, text: str) -> None:
    """A settings file must say `schema = 3`; anything else is refused with no special handling."""
    world.user_file(text)

    with pytest.raises(SettingsError, match="schema|unknown key"):
        world.load()


def test_roots_must_be_absolute_distinct_and_not_nested(world: World) -> None:
    """Relative, equal or nested roots are rejected, and an undefined %VARIABLE% is reported."""
    nested = world.user / "inner"
    for text in (
        "schema = 3\n[roots]\nuser = 'relative\\opt'\n",
        f"schema = 3\n[roots]\nportable = '{world.user}'\n",
        f"schema = 3\n[roots]\nportable = '{nested}'\n",
        "schema = 3\n[roots]\nportable = '%NOT_DEFINED%\\x'\n",
    ):
        world.user_file(text)
        with pytest.raises(SettingsError):
            world.load()


def test_invalid_toml_names_the_file(world: World) -> None:
    """A file that is not TOML is an error that names the layer and the file."""
    world.user_file("schema = [")

    with pytest.raises(SettingsError, match="not valid TOML"):
        world.load()
