"""Cover the schema 4 settings file, the root fallback chain and the layers of one run.

Behavior covered: each mode finds its root by a fixed chain of fallbacks (4 rows in system mode, 5 in user mode) and reports
the source of the root it chose; every effective value reports the layer it came from; later layers override earlier
ones; ``[roots]`` is refused in a root's own ``settings.toml``; ``--settings`` is refused in system mode; a system-mode run
refuses a settings file in a folder that is not admin-only; a user run never reads anything under the system root; a
``schema = 3`` file is refused with the migration message; nothing is ever loaded from AppData or ProgramData; invalid files
are reported with every problem and a hint.

Real: ``gupkg.settingsload.resolve_root``, ``load_mode_settings`` and the model, on settings files written under pytest's
temporary folder, and (one ``process`` test) the real administrator-only folder check. Fake: ``FakeSystem``
answers that check in the other tests, which would otherwise need an administrator-owned folder. Out of scope: the
``gupkg settings show`` command itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gupkg.model.settings import SettingsError, default_layer, merge_layers
from gupkg.settingsload import load_mode_settings, resolve_root, settings_file
from gupkg.system.fake import FakeSystem
from gupkg.system.windows import WindowsSystem

pytestmark = pytest.mark.unit


class World:
    """A fake machine under a temporary folder: roots, an app folder and an environment."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.system = base / "sys"
        self.user = base / "home" / "opt"
        self.app = base / "app"
        # Environment variables that decide where the profile folders are.
        self.env = {
            "USERPROFILE": str(base / "home"),
            "APPDATA": str(base / "appdata"),
            "LOCALAPPDATA": str(base / "localappdata"),
            "ProgramData": str(base / "programdata"),
        }
        self.reads: list[Path] = []

    def fake(self, *, untrusted: bool = False) -> FakeSystem:
        """Return a machine on which every folder is admin-only, or (with *untrusted*) none below the test folder is."""
        # FakeSystem treats every folder as admin-only unless it is listed as untrusted.
        return FakeSystem(self.base / "fake", untrusted_dirs={self.base} if untrusted else set())

    def app_file(self, extra: str = "") -> Path:
        """Write ``$App\\settings.toml``."""
        return self.write(self.app / "settings.toml", "schema = 4\n" + extra)

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

    def load(self, mode: str = "user", *, root: Path | None = None, settings_path: Path | None = None,
             untrusted: bool = False, system: FakeSystem | None = None, app: bool = True):
        """Load the settings of a run in *mode* on its default test root (on *system* when one is given)."""
        return load_mode_settings(
            mode, root or (self.system if mode == "system" else self.user),
            app_dir=self.app if app else None, settings_path=settings_path,
            system=system or self.fake(untrusted=untrusted), env=self.env, read_text=self.read_text,
        )


@pytest.fixture
def settings_world(tmp_path: Path) -> World:
    """A fake machine whose roots, app folder and AppData live under a temporary folder."""
    return World(tmp_path)


def sources(loaded) -> dict[str, str]:
    """Return {key: layer name} for every effective value."""
    return {row.key: row.layer for row in loaded.settings.effective()}


# ---------------------------------------------------------------------------
# The root fallback chain
# ---------------------------------------------------------------------------


def roots_data(**roots: Path | str) -> dict:
    """Return the parsed ``[roots]`` table of a layer."""
    return {"roots": {name: str(value) for name, value in roots.items()}}


def choose(world: World, mode: str, *, option: Path | None = None, installed: Path | None = None,
           app: dict | None = None, cli: dict | None = None, untrusted: bool = False):
    """Call ``resolve_root`` with what the test sets; the rest is absent."""
    return resolve_root(
        mode, root_option=option, installed_root=installed, app_layer=app, cli_layer=cli,
        env=world.env, system=world.fake(untrusted=untrusted),
    )


def test_system_row_1_root_option_wins(settings_world: World) -> None:
    """``--root`` beats the installed system gupkg's root, ``$App`` and the built-in default."""
    chosen = choose(settings_world, "system", option=settings_world.base / "r1", installed=settings_world.base / "r2",
                    app=roots_data(system=settings_world.base / "r3"))

    assert (chosen.path, chosen.source) == (settings_world.base / "r1", "--root")


def test_system_row_2_installed_system_root_wins(settings_world: World) -> None:
    """The root of the installed system gupkg beats ``$App\\settings.toml`` and the built-in default."""
    chosen = choose(settings_world, "system", installed=settings_world.base / "r2", app=roots_data(system=settings_world.base / "r3"))

    assert (chosen.path, chosen.source) == (settings_world.base / "r2", "the installed system gupkg")


def test_system_row_3_app_settings_win(settings_world: World) -> None:
    """``[roots].system`` of ``$App\\settings.toml`` beats the built-in default."""
    chosen = choose(settings_world, "system", app=roots_data(system=settings_world.base / "r3"))

    assert (chosen.path, chosen.source) == (settings_world.base / "r3", "$App\\settings.toml")


def test_system_row_4_built_in_default(settings_world: World) -> None:
    """With nothing else the system root is ``C:\\opt``."""
    chosen = choose(settings_world, "system")

    assert (chosen.path, chosen.source) == (Path("C:\\opt"), "built-in default")


def test_user_row_1_root_option_wins(settings_world: World) -> None:
    """``--root`` beats every other source of the user root."""
    chosen = choose(settings_world, "user", option=settings_world.base / "r1", cli=roots_data(user=settings_world.base / "r2"),
                    installed=settings_world.base / "r3", app=roots_data(user=settings_world.base / "r4"), untrusted=True)

    assert (chosen.path, chosen.source) == (settings_world.base / "r1", "--root")


def test_user_row_2_settings_option_wins(settings_world: World) -> None:
    """``[roots].user`` of ``--settings`` beats the installed root, ``$App`` and the built-in default."""
    chosen = choose(settings_world, "user", cli=roots_data(user=settings_world.base / "r2"), installed=settings_world.base / "r3",
                    app=roots_data(user=settings_world.base / "r4"), untrusted=True)

    assert (chosen.path, chosen.source) == (settings_world.base / "r2", "--settings")


def test_user_row_3_installed_root_wins_when_it_is_not_admin_only(settings_world: World) -> None:
    """A gupkg installed in a root that users can write manages that root, before ``$App`` and the built-in default."""
    chosen = choose(settings_world, "user", installed=settings_world.base / "r3", app=roots_data(user=settings_world.base / "r4"), untrusted=True)

    assert (chosen.path, chosen.source) == (settings_world.base / "r3", "the root this gupkg is installed in")


def test_user_row_3_is_skipped_for_an_admin_only_installed_root(settings_world: World) -> None:
    """The system gupkg run by a standard user does not hand its admin-only root to the user mode (no system data is read)."""
    chosen = choose(settings_world, "user", installed=settings_world.base / "r3", app=roots_data(user=settings_world.base / "r4"), untrusted=False)

    assert (chosen.path, chosen.source) == (settings_world.base / "r4", "$App\\settings.toml")


def test_user_row_4_app_settings_win(settings_world: World) -> None:
    """``[roots].user`` of ``$App\\settings.toml`` beats the built-in default (a source tree has no installed root)."""
    chosen = choose(settings_world, "user", app=roots_data(user=settings_world.base / "r4"))

    assert (chosen.path, chosen.source) == (settings_world.base / "r4", "$App\\settings.toml")


def test_user_row_5_built_in_default(settings_world: World) -> None:
    """With nothing else the user root is ``%USERPROFILE%\\opt``."""
    chosen = choose(settings_world, "user")

    assert (chosen.path, chosen.source) == (settings_world.base / "home" / "opt", "built-in default")


def test_a_mode_reads_only_its_own_roots_key(settings_world: World) -> None:
    """The system mode ignores ``[roots].user`` and the user mode ignores ``[roots].system``."""
    both = roots_data(system=settings_world.base / "s", user=settings_world.base / "u")

    assert choose(settings_world, "system", app=both).path == settings_world.base / "s"
    assert choose(settings_world, "user", app=both).path == settings_world.base / "u"


def test_root_option_in_system_mode_must_be_admin_only(settings_world: World) -> None:
    """``--root`` for the system mode in a folder that users can write is refused, with the reason."""
    with pytest.raises(SettingsError, match="--root .* is not admin-only"):
        choose(settings_world, "system", option=settings_world.base / "open", untrusted=True)


def test_a_relative_root_is_refused(settings_world: World) -> None:
    """A root must be an absolute path with a drive."""
    with pytest.raises(SettingsError, match="absolute path with a drive"):
        choose(settings_world, "user", app=roots_data(user="relative\\opt"))


def test_an_undefined_variable_in_a_root_is_reported(settings_world: World) -> None:
    """A ``%VARIABLE%`` that the environment lacks is named in the error."""
    with pytest.raises(SettingsError, match="NOT_DEFINED"):
        choose(settings_world, "user", app=roots_data(user="%NOT_DEFINED%\\x"))


# ---------------------------------------------------------------------------
# The rules of the layers
# ---------------------------------------------------------------------------


def test_defaults_alone_give_the_documented_values_and_say_so() -> None:
    """With only the built-in defaults the documented defaults apply and every value is attributed to `defaults`."""
    user = merge_layers([default_layer("user")])
    system = merge_layers([default_layer("system")])

    assert user["roots.system"] == "C:\\opt"
    assert (user["integration.integrate"], user["integration.bin"]) == ("full", "%USERPROFILE%\\bin")
    assert system["integration.bin"] == "C:\\bin"
    assert user["manager.checksum_missing"] == "ask"
    assert [(r.name, r.source) for r in user.registries] == [("main", "https://github.com/guraltsev/gupkg-registry.git")]
    assert {row.layer for row in user.effective()} == {"defaults"}


def test_each_effective_value_reports_the_layer_it_came_from(settings_world: World) -> None:
    """Values set by the app, root and command-line layers are attributed to exactly those layers."""
    settings_world.app_file('[logs]\nkeep_runs = 7\nkeep_days = 5\n')
    settings_world.user_file('schema = 4\n[logs]\nkeep_runs = 9\n[ui]\ncolor = "never"\n')
    cli = settings_world.write(settings_world.base / "cli.toml", 'schema = 4\n[cleanup]\nauto = true\n')

    loaded = settings_world.load(settings_path=cli)
    origin = sources(loaded)

    assert loaded.settings["logs.keep_runs"] == 9          # the root beats $App
    assert (origin["logs.keep_runs"], origin["logs.keep_days"]) == ("root", "app")
    assert (origin["ui.color"], origin["cleanup.auto"]) == ("root", "cli")
    assert origin["manager.keep_screen"] == "defaults"
    assert [(r.name, r.status) for r in loaded.layers] == [
        ("defaults", "applied"), ("app", "applied"), ("root", "applied"), ("cli", "applied"),
    ]


def test_registry_array_is_replaced_by_the_highest_layer_that_sets_it(settings_world: World) -> None:
    """A `[[registry]]` array in a later layer replaces the earlier one entirely."""
    settings_world.user_file('schema = 4\n[[registry]]\nname = "team"\nsource = "https://example.org/team.git"\n')

    loaded = settings_world.load()

    assert [(r.name, r.source) for r in loaded.settings.registries] == [("team", "https://example.org/team.git")]
    assert sources(loaded)["registry"] == "root"


def test_roots_in_a_root_layer_is_refused_and_names_the_file(settings_world: World) -> None:
    """``[roots]`` in ``<root>\\.gupkg\\settings.toml`` is an error that names the file and says where to set it."""
    path = settings_world.user_file(f"schema = 4\n[roots]\nuser = '{settings_world.base / 'elsewhere'}'\n")

    with pytest.raises(SettingsError) as caught:
        settings_world.load()

    assert str(caught.value) == (
        f"[roots] cannot be set in a root's own settings.toml ({path}); set it in $App\\settings.toml or with --root"
    )


def test_roots_in_the_app_and_settings_layers_are_accepted(settings_world: World) -> None:
    """``$App\\settings.toml`` and ``--settings`` may carry ``[roots]``."""
    settings_world.app_file(f"[roots]\nsystem = '{settings_world.system}'\n")
    cli = settings_world.write(settings_world.base / "cli.toml", f"schema = 4\n[roots]\nuser = '{settings_world.user}'\n")

    loaded = settings_world.load(settings_path=cli)

    assert (loaded.settings["roots.system"], loaded.settings["roots.user"]) == (str(settings_world.system), str(settings_world.user))


def test_the_settings_option_is_refused_in_system_mode(settings_world: World) -> None:
    """The system mode takes no ``--settings`` file."""
    extra = settings_world.write(settings_world.system / "extra.toml", "schema = 4\n")

    with pytest.raises(SettingsError, match="--settings is not accepted in system mode"):
        settings_world.load("system", settings_path=extra)


def test_settings_file_anywhere_is_used_in_user_mode_and_must_exist(settings_world: World) -> None:
    """User mode takes `--settings` from any folder, and a missing file is an error rather than silence."""
    elsewhere = settings_world.write(settings_world.base / "elsewhere.toml", 'schema = 4\n[ui]\ncolor = "never"\n')

    assert settings_world.load(settings_path=elsewhere, untrusted=True).settings["ui.color"] == "never"
    with pytest.raises(SettingsError, match="does not exist"):
        settings_world.load(settings_path=settings_world.base / "absent.toml")


def test_a_user_run_never_reads_the_system_root(settings_world: World) -> None:
    """Nothing under the system root is read or even asked for when the user mode loads its settings."""
    settings_world.system_file('schema = 4\n[ui]\ncolor = "never"\n')
    settings_world.app_file(f"[roots]\nsystem = '{settings_world.system}'\nuser = '{settings_world.user}'\n")
    cli = settings_world.write(settings_world.base / "cli.toml", "schema = 4\n")
    choice = choose(settings_world, "user", app=roots_data(system=settings_world.system, user=settings_world.user), cli=roots_data())

    loaded = settings_world.load(root=choice.path, settings_path=cli)

    assert loaded.settings["ui.color"] == "auto"
    assert settings_world.reads and not [path for path in settings_world.reads if settings_world.system in path.parents or path == settings_world.system]


def test_a_system_run_refuses_a_layer_in_a_folder_that_is_not_admin_only(settings_world: World) -> None:
    """A settings file in a folder a standard user can change stops the system mode instead of being trusted."""
    settings_world.system_file('schema = 4\n[ui]\ncolor = "never"\n')

    with pytest.raises(SettingsError, match="Refusing the"):
        settings_world.load("system", untrusted=True)


def test_a_system_run_refuses_an_app_layer_that_is_not_admin_only(settings_world: World) -> None:
    """``$App\\settings.toml`` is checked like the root's file."""
    settings_world.app_file()
    trusted_root = settings_world.base.parent / "other-root"
    system = FakeSystem(settings_world.base / "fake", untrusted_dirs={settings_world.app})

    with pytest.raises(SettingsError, match="Refusing the app settings"):
        settings_world.load("system", root=trusted_root, system=system)


def test_a_user_run_does_not_need_admin_only_folders(settings_world: World) -> None:
    """A user-mode run reads layers from any folder without the admin-only check."""
    settings_world.user_file('schema = 4\n[ui]\ncolor = "never"\n')

    assert settings_world.load(untrusted=True).settings["ui.color"] == "never"


def test_nothing_is_read_from_appdata_or_programdata(settings_world: World) -> None:
    """Settings files planted in AppData, LocalAppData and ProgramData are never read and never applied."""
    for variable in ("APPDATA", "LOCALAPPDATA", "ProgramData"):
        for relative in ("settings.toml", "gupkg/settings.toml", ".gupkg/settings.toml"):
            settings_world.write(Path(settings_world.env[variable]) / relative, 'schema = 4\n[ui]\ncolor = "never"\n')

    loaded = settings_world.load()

    assert loaded.settings["ui.color"] == "auto"
    forbidden = [Path(settings_world.env[v]) for v in ("APPDATA", "LOCALAPPDATA", "ProgramData")]
    assert not [read for read in settings_world.reads if any(parent in forbidden for parent in read.parents)]


def test_missing_layers_are_skipped_and_reported(settings_world: World) -> None:
    """Absent app and root files are reported as missing and change nothing."""
    loaded = settings_world.load()

    statuses = {r.name: r.status for r in loaded.layers}
    assert statuses["app"] == "missing" and statuses["root"] == "missing"
    assert loaded.settings["roots.system"] == "C:\\opt"


def test_invalid_layer_lists_every_problem_with_hints(settings_world: World) -> None:
    """An invalid file reports all its problems at once and names the file and the likely key."""
    settings_world.user_file(
        'schema = 4\nrotos = "user"\n[ui]\ncolor = "purple"\n[logs]\nkeep_runs = 0\n[[registry]]\nname = "a"\n'
    )

    with pytest.raises(SettingsError) as caught:
        settings_world.load()

    text = str(caught.value)
    assert len(caught.value.problems) >= 4
    assert "did you mean 'roots'" in text
    assert "settings.toml" in text
    assert "'ui.color' must be one of auto, always, never" in text


def test_a_schema_3_file_is_refused_with_the_migration_message(settings_world: World) -> None:
    """A schema 3 file is not converted; the error says what to change."""
    settings_world.user_file('schema = 3\n[logs]\nkeep_runs = 7\n')

    with pytest.raises(SettingsError, match=r"settings schema 3 is no longer read; change it to schema = 4 \(see _docs/rehaul_design.md §11\)"):
        settings_world.load()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("[ui]\ncolor = 'auto'\n", id="no-schema"),
        pytest.param("schema = 2\n", id="schema-2"),
        pytest.param('mode = "manager"\nschema_version = 2\n', id="manager-config"),
    ],
)
def test_files_that_are_not_schema_4_are_refused(settings_world: World, text: str) -> None:
    """A settings file must say `schema = 4` and use only the schema 4 keys."""
    settings_world.user_file(text)

    with pytest.raises(SettingsError, match="schema|unknown key"):
        settings_world.load()


def test_the_folder_is_checked_before_the_file_is_read(settings_world: World) -> None:
    """In system mode a file in an untrusted folder is refused without having been read."""
    system_file = settings_world.system_file('schema = 4\n[ui]\ncolor = "never"\n')

    with pytest.raises(SettingsError, match="Refusing"):
        settings_world.load("system", untrusted=True)

    assert system_file not in settings_world.reads


def test_a_missing_layer_in_an_untrusted_folder_is_still_refused_in_system_mode(settings_world: World) -> None:
    """The folder check covers a settings file that does not exist yet, judged by its nearest existing parent."""
    with pytest.raises(SettingsError, match="Refusing"):
        settings_world.load("system", untrusted=True)


def test_a_folder_that_cannot_be_judged_stops_a_system_run_with_a_settings_error(settings_world: World) -> None:
    """When the boundary cannot say whether a folder is admin-only (it raises), the run is refused, not crashed or trusted."""
    settings_world.system_file('schema = 4\n[ui]\ncolor = "never"\n')

    class Unjudgeable(FakeSystem):
        """A machine whose permission check fails."""

        def directory_untrusted_reason(self, path):
            """Fail as if access to the folder were denied."""
            raise PermissionError("access denied")

    with pytest.raises(SettingsError, match="could not be judged admin-only: access denied"):
        settings_world.load("system", system=Unjudgeable(settings_world.base / "fake2"))


def test_a_user_run_does_not_ask_whether_folders_are_admin_only(settings_world: World) -> None:
    """In user mode the permission check is never made (a failing boundary changes nothing)."""
    settings_world.user_file('schema = 4\n[ui]\ncolor = "never"\n')

    class Unjudgeable(FakeSystem):
        """A machine whose permission check fails."""

        def directory_untrusted_reason(self, path):
            """Fail as if access to the folder were denied."""
            raise PermissionError("access denied")

    assert settings_world.load(system=Unjudgeable(settings_world.base / "fake2")).settings["ui.color"] == "never"


def test_unreadable_and_undecodable_files_are_errors_not_skipped_layers(settings_world: World) -> None:
    """An OS error other than 'not found' and a file that is not UTF-8 both stop the run with a named error."""
    settings_world.user_file("schema = 4\n")
    settings_file(settings_world.user).write_bytes(b"schema = 4\n# \xff\xfe\n")

    with pytest.raises(SettingsError, match="cannot read the root settings"):
        load_mode_settings("user", settings_world.user, app_dir=None, settings_path=None, system=settings_world.fake(), env=settings_world.env)

    # Make the folder-trust check fail as if access were denied.
    def broken(path: Path) -> str | None:
        """Fail as if access to the folder were denied."""
        raise PermissionError("denied")

    with pytest.raises(SettingsError, match="cannot read the app settings"):
        load_mode_settings(
            "user", settings_world.user, app_dir=settings_world.app, settings_path=None, system=settings_world.fake(), env=settings_world.env, read_text=broken,
        )


def test_invalid_toml_names_the_file(settings_world: World) -> None:
    """A file that is not TOML is an error that names the layer and the file."""
    settings_world.user_file("schema = [")

    with pytest.raises(SettingsError, match="not valid TOML"):
        settings_world.load()


@pytest.mark.process
@pytest.mark.skipif(os.name != "nt", reason="reads Windows access-control lists")
def test_real_admin_only_check_refuses_a_user_writable_system_layer(settings_world: World) -> None:
    """With the real folder check, a system-mode run refuses a root layer in a folder the current user can write."""
    settings_world.system_file('schema = 4\n[ui]\ncolor = "never"\n')

    with pytest.raises(SettingsError, match="Refusing the root settings"):
        load_mode_settings("system", settings_world.system, app_dir=None, settings_path=None, system=WindowsSystem(), env=settings_world.env)
