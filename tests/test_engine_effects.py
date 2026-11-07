"""Cover the integration effects: how declarations become effects, and how effects are applied, removed and verified.

Behaviour covered: variable expansion (``$App``, ``$PackageRoot``, ``$Bin``, ``$$``, ``${NAME}``) and its errors; shortcuts and
shims confined to the Start Menu and ``bin`` folders; a mode without a Start Menu or an environment target leaves those
rows out and says so; apply and remove of every kind through ``FakeSystem``;
a hostile install record cannot make gupkg delete or write outside the mode's folders; a ``PATH`` entry is removed only when this install
added it and never when it is the shared ``bin`` folder; verification reads only what gupkg wrote and skips the user's
environment in an elevated run.

Real: the effect code and real folders. Mocked: ``FakeSystem`` (registry, shortcuts). Out of scope: the install flow around it.
"""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from gupkg.engine.effects import (
    EffectContext,
    EffectError,
    apply_effect,
    desired_effects,
    remove_effect,
    shim_config_text,
    shortcut_differences,
    verify_each,
)
from gupkg.model.manifest import EnvVar, Shim, Shortcut, parse_manifest
from gupkg.model.state import Effect
from gupkg.system.fake import FakeSystem
from gupkg.system.port import Mode, ShortcutSpec

pytestmark = pytest.mark.unit


def context(tmp_path: Path, *, mode: str = "user", start_menu: bool = True, scope: Mode | None = Mode.USER, env=None) -> EffectContext:
    """Return an effect context below *tmp_path*."""
    package = tmp_path / "opt" / "tool"
    return EffectContext(
        package, mode, tmp_path / "bin", tmp_path / "menu" if start_menu else None, scope, env or {"USERPROFILE": "C:\\Users\\me"},
    )


def manifest(text: str):
    """Parse a version manifest with a download source and *text* after it."""
    return parse_manifest('format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n' + text)


def test_variables_expand_against_current_so_shortcuts_survive_a_switch(tmp_path: Path) -> None:
    """`$App` and friends point below `<package>\\current`; `${NAME}` comes from the environment; `$$` is a dollar sign."""
    ctx = context(tmp_path)
    effects, _ = desired_effects(manifest(
        '[[env]]\nname = "A"\nvalue = "$App;$VersionRoot;$PackageRoot;$Bin;${USERPROFILE};$$"\n'
    ), ctx)

    [env] = effects
    package = tmp_path / "opt" / "tool"
    assert env.fields["value"] == (
        f"{package / 'current' / 'App'};{package / 'current'};{package};{tmp_path / 'bin'};C:\\Users\\me;$"
    )


@pytest.mark.parametrize(
    ("row", "message"),
    [
        pytest.param('[[env]]\nname = "A"\nvalue = "${NOPE}"\n', "not defined", id="unknown-environment-reference"),
        pytest.param('[[shortcut]]\nname = "..\\\\..\\\\evil"\ntarget = "$App\\\\a.exe"\n', "outside", id="shortcut-escapes-the-menu"),
        pytest.param('[[shortcut]]\nname = "C:\\\\evil"\ntarget = "$App\\\\a.exe"\n', "relative", id="absolute-shortcut-name"),
        pytest.param('[[shortcut]]\nname = "Tool:ads"\ntarget = "$App\\\\a.exe"\n', "stream", id="shortcut-ntfs-stream"),
        pytest.param('[[shortcut]]\nname = "NUL"\ntarget = "$App\\\\a.exe"\n', "device name", id="shortcut-nul"),
        pytest.param('[[shortcut]]\nname = "aux.lnk"\ntarget = "$App\\\\a.exe"\n', "device name", id="shortcut-aux-lnk"),
        pytest.param('[[shortcut]]\nname = "COM1"\ntarget = "$App\\\\a.exe"\n', "device name", id="shortcut-com1"),
        pytest.param('[[shortcut]]\nname = "Group\\\\con"\ntarget = "$App\\\\a.exe"\n', "device name", id="shortcut-device-in-folder"),
    ],
)
def test_a_declaration_that_cannot_become_an_effect_is_refused(tmp_path: Path, row: str, message: str) -> None:
    """Unknown references, a name that leaves the Start Menu are errors, not guesses."""
    with pytest.raises(EffectError, match=message):
        desired_effects(manifest(row), context(tmp_path))


def test_a_mode_without_a_start_menu_or_an_environment_target_leaves_those_rows_out(tmp_path: Path) -> None:
    """User `integrate = "none"`: shims stay, shortcuts, environment values and PATH entries are skipped and reported."""
    text = (
        '[[shortcut]]\nname = "T"\ntarget = "$App\\\\a.exe"\n[[shim]]\nname = "t"\ntarget = "$App\\\\a.exe"\n'
        '[[env]]\nname = "A"\nvalue = "1"\n[[path]]\nvalue = "$App"\n'
    )

    effects, notes = desired_effects(manifest(text), context(tmp_path, start_menu=False, scope=None))

    assert [effect.kind for effect in effects] == ["shim"] and len(notes) == 3
    assert any("gupkg shell" in note for note in notes)


def test_two_declarations_of_the_same_thing_are_an_error(tmp_path: Path) -> None:
    """A shortcut declared twice with different targets would silently lose one: the package is refused."""
    text = '[[shortcut]]\nname = "T"\ntarget = "$App\\\\a.exe"\n[[shortcut]]\nname = "t"\ntarget = "$App\\\\b.exe"\n'

    with pytest.raises(EffectError, match="same shortcut"):
        desired_effects(manifest(text), context(tmp_path))


def test_every_kind_is_applied_verified_and_removed(tmp_path: Path, system: FakeSystem) -> None:
    """apply creates the shortcut, the shim files, the environment value and the PATH entry; verify finds them; remove deletes them."""
    # Arrange: a manifest with a shortcut, shims, a variable and a PATH entry.
    ctx = context(tmp_path)
    text = (
        '[[shortcut]]\nname = "Group\\\\T"\ntarget = "$App\\\\a.exe"\n[[shim]]\nname = "t"\ntarget = "$App\\\\a.exe"\n'
        '[[shim]]\nname = "run.cmd"\ncontent = "@echo off\\r\\necho %~dp0\\r\\n"\n'
        '[[env]]\nname = "A"\nvalue = "1"\n[[path]]\nvalue = "$App"\n'
    )
    effects, _ = desired_effects(manifest(text), ctx)

    # Act: apply every effect.
    applied = [apply_effect(system, effect, ctx) for effect in effects]

    # Assert: the effects exist and verify.
    assert (tmp_path / "bin" / "t.exe").is_file() and (tmp_path / "bin" / "t.config.toml").is_file()
    assert (tmp_path / "bin" / "run.cmd").read_bytes() == b"@echo off\r\necho %~dp0\r\n"
    assert [item for item in verify_each(system, applied, mode=Mode.USER) if item[1] or item[2]] == []
    assert next(item for item in applied if item.kind == "path").fields["added"] is True
    removed = {(effect.kind, bool(effect.fields.get("shared"))): remove_effect(system, effect, ctx) for effect in applied}
    assert all(done for (kind, shared), done in removed.items() if not shared) and removed[("path", True)] is False
    # Remove them again and verify that the removal is seen.
    assert not (tmp_path / "bin" / "t.exe").exists() and not (tmp_path / "menu" / "Group").exists()
    assert system.get_path(Mode.USER) == [str(tmp_path / "bin")] and "A" not in system.env_values(Mode.USER)
    assert [item[0].kind for item in verify_each(system, applied, mode=Mode.USER) if item[1]] == ["shortcut", "shim", "file", "env", "path"]


def test_shortcut_drift_names_the_field_that_differs_and_ignores_letter_case_in_paths(tmp_path: Path, system: FakeSystem) -> None:
    """A shortcut with another icon or description is drift and the message says which; one that only spells a path differently is not."""
    # Arrange: apply a shortcut.
    ctx = context(tmp_path)
    effects, _ = desired_effects(manifest('[[shortcut]]\nname = "T"\ntarget = "$App\\\\a.exe"\ndescription = "Tool"\n'), ctx)
    effect = apply_effect(system, effects[0], ctx)
    path = Path(str(effect.fields["path"]))
    stored = system.read_shortcut(path)

    # Only a difference in letter case of a path is ignored.
    system.write_shortcut(path, replace(stored, target_path=stored.target_path.upper()))
    assert [row for row in verify_each(system, [effect], mode=Mode.USER) if row[1] or row[2]] == []

    # Another icon and description are drift, and the message names the fields.
    system.write_shortcut(path, replace(stored, icon_location="C:\\other.ico,0", description="Other"))
    drift = [row[1] for row in verify_each(system, [effect], mode=Mode.USER) if row[1]]
    assert len(drift) == 1 and drift[0].startswith(f"shortcut differs: {path} (icon is 'C:\\other.ico,0' but would be ''; ")
    assert "description is 'Other' but would be 'Tool'" in drift[0]


def test_a_shortcut_as_windows_reads_it_back_has_no_differences() -> None:
    """Windows returns an icon as ``path,index`` and cuts a description at 260 characters; another icon index is still a difference."""
    fields = {"target": "C:\\a.exe", "icon_location": "C:\\a.exe", "description": "d" * 300}
    stored = ShortcutSpec("C:\\a.exe", icon_location="C:\\a.exe,0", description="d" * 260)

    assert shortcut_differences(stored, fields) == []
    assert shortcut_differences(replace(stored, icon_location="C:\\a.exe,1"), fields) == ["icon is 'C:\\a.exe,1' but would be 'C:\\a.exe'"]


def test_a_path_entry_that_was_already_there_is_never_removed(tmp_path: Path, system: FakeSystem) -> None:
    """Only an entry this install added is removed; one that was on PATH before stays when the package goes."""
    ctx = context(tmp_path)
    entry = str(tmp_path / "opt" / "tool" / "current" / "App")
    system.set_path(Mode.USER, [entry])
    [effect], _ = desired_effects(manifest('[[path]]\nvalue = "$App"\n'), ctx)

    applied = apply_effect(system, effect, ctx)

    assert applied.fields["added"] is False
    assert remove_effect(system, applied, ctx) is False and system.get_path(Mode.USER) == [entry]


def test_the_shared_bin_entry_is_never_removed_by_one_package(tmp_path: Path, system: FakeSystem) -> None:
    """The bin folder belongs to the mode and other packages' shims need it: removing one package leaves it on PATH."""
    ctx = context(tmp_path)
    effects, _ = desired_effects(manifest('[[shim]]\nname = "t"\ntarget = "$App\\\\a.exe"\n'), ctx)
    applied = [apply_effect(system, effect, ctx) for effect in effects]
    entry = next(item for item in applied if item.kind == "path")
    assert entry.fields["added"] is True and entry.fields["shared"] is True

    assert remove_effect(system, entry, ctx) is False and str(tmp_path / "bin") in system.get_path(Mode.USER)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(Effect("shim", {"path": "C:\\Windows\\System32\\cmd.exe", "config": "C:\\Windows\\System32\\x.toml"}), id="shim-outside-bin"),
        pytest.param(Effect("file", {"path": "C:\\Windows\\win.ini"}), id="file-outside-bin"),
        pytest.param(Effect("shortcut", {"path": "C:\\Users\\Public\\Desktop\\x.lnk"}), id="shortcut-outside-menu"),
        pytest.param(Effect("shim", {"path": "{bin}", "config": "{bin}"}), id="the-bin-folder-itself"),
    ],
)
def test_a_hostile_install_record_cannot_remove_anything_outside_the_modes_folders(tmp_path: Path, system: FakeSystem, bad: Effect) -> None:
    """Removal checks the recorded path against the folder the mode owns, whatever the record claims."""
    ctx = context(tmp_path)
    ctx.bin_dir.mkdir()
    fields = {key: value.replace("{bin}", str(ctx.bin_dir)) for key, value in bad.fields.items()}

    with pytest.raises(EffectError, match="refusing|outside"):
        remove_effect(system, Effect(bad.kind, fields), ctx)


def test_apply_refuses_a_recorded_effect_that_leaves_its_folder(tmp_path: Path, system: FakeSystem) -> None:
    """The same guard stands in front of every write: an effect naming a path outside the folder is refused, nothing is written."""
    ctx = context(tmp_path)
    outside = tmp_path / "elsewhere" / "x.exe"

    with pytest.raises(EffectError):
        apply_effect(system, Effect("shim", {"path": str(outside), "config": str(outside) + ".toml", "target": "t"}), ctx)

    assert not outside.exists()


def test_verification_reads_the_machine_environment_only_for_what_gupkg_wrote(tmp_path: Path, system: FakeSystem) -> None:
    """Verifying an installed package reads the values and the PATH of its record and nothing else."""
    ctx = context(tmp_path)
    effects, _ = desired_effects(manifest('[[env]]\nname = "A"\nvalue = "1"\n[[path]]\nvalue = "$App"\n'), ctx)
    applied = [apply_effect(system, effect, ctx) for effect in effects]
    system.registry_log.clear()

    verify_each(system, applied, mode=Mode.USER)
    assert {(item.scope, item.name) for item in system.registry_log if item.operation == "read"} == {("user", "A"), ("user", "Path")}


def test_a_system_mode_run_never_reads_the_user_environment_for_a_user_scope_effect(tmp_path: Path, system: FakeSystem) -> None:
    """A user-scope env or PATH effect of a record is reported unverified in a system-mode run, and the user environment is not read."""
    ctx = context(tmp_path)
    effects, _ = desired_effects(manifest('[[env]]\nname = "A"\nvalue = "1"\n[[path]]\nvalue = "$App"\n'), ctx)
    applied = [apply_effect(system, effect, ctx) for effect in effects]
    system.registry_log.clear()

    rows =verify_each(system, applied, mode=Mode.SYSTEM)

    assert system.registry_log == [] and all(unverified and not drift for _, drift, unverified in rows)


@pytest.mark.parametrize("name", ["Path", "PATH", "TEMP", "ComSpec"])
def test_a_reserved_variable_is_never_set_or_deleted_by_an_effect(tmp_path: Path, system: FakeSystem, name: str) -> None:
    """Defence in depth: a recorded `env` effect named like a system variable is refused on apply and on removal, and PATH is untouched."""
    ctx = context(tmp_path)
    system.set_path(Mode.USER, ["C:\\A", "C:\\B"])
    effect = Effect("env", {"scope": "user", "name": name, "value": "C:\\only"})

    with pytest.raises(EffectError, match="reserved"):
        apply_effect(system, effect, ctx)
    with pytest.raises(EffectError, match="reserved"):
        remove_effect(system, effect, ctx)

    assert system.get_path(Mode.USER) == ["C:\\A", "C:\\B"]


def test_a_manifest_with_a_reserved_variable_cannot_become_effects(tmp_path: Path) -> None:
    """A manifest built without the parser (a hostile version directory) is still refused when the effects are derived."""
    ctx = context(tmp_path)
    parsed = manifest('[[env]]\nname = "A"\nvalue = "1"\n')
    hostile = replace(parsed, envs=(EnvVar("Path", "C:\\only"),))

    with pytest.raises(EffectError, match="reserved"):
        desired_effects(hostile, ctx)


@pytest.mark.parametrize("name", ["A" * 129, "A%B", "A​B", "A\u0007B"])
def test_a_long_or_invisible_variable_name_is_never_set_or_deleted(tmp_path: Path, system: FakeSystem, name: str) -> None:
    """The name rules hold below the validator too: apply and remove refuse, and desired_effects refuses a hostile manifest."""
    ctx = context(tmp_path)
    effect = Effect("env", {"scope": "user", "name": name, "value": "x"})

    with pytest.raises(EffectError):
        apply_effect(system, effect, ctx)
    with pytest.raises(EffectError):
        remove_effect(system, effect, ctx)
    with pytest.raises(EffectError):
        desired_effects(replace(manifest(""), envs=(EnvVar(name, "x"),)), ctx)


@pytest.mark.parametrize("kind", ["shortcut", "shim"])
def test_a_name_of_256_characters_cannot_become_an_effect(tmp_path: Path, kind: str) -> None:
    """A hostile manifest with a 256-character shortcut or shim name is refused when the effects are derived."""
    ctx = context(tmp_path)
    long = "a" * 256
    hostile = replace(manifest(""), **({"shortcuts": (Shortcut(long, "$App\\a.exe"),)} if kind == "shortcut" else {"shims": (Shim(name=long, target="$App\\a.exe"),)}))

    with pytest.raises(EffectError, match="longer"):
        desired_effects(hostile, ctx)


# Table that declares the shim of the test package.
SHIM = '[[shim]]\nname = "t"\ntarget = "$App\\\\a.exe"\n'


def test_a_dynamic_shim_gets_its_libraries_in_exe_shim_lib_and_none_loose_in_bin(tmp_path: Path, system: FakeSystem) -> None:
    """The launcher loads its DLLs from the ``exe-shim-lib`` folder in ``bin`` only, so the DLLs, the assembly manifest and the licenses go there."""
    ctx = context(tmp_path)
    effect = next(item for item in desired_effects(manifest(SHIM), ctx)[0] if item.kind == "shim")

    apply_effect(system, effect, ctx)

    library = tmp_path / "bin" / "exe-shim-lib"
    assert {item.name for item in library.iterdir()} >= {"libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll", "exe-shim-lib.manifest"}
    assert not list((tmp_path / "bin").glob("*.dll"))
    assert (tmp_path / "bin" / "LICENSE-exe-shim-MIT.txt").is_file()


def test_a_searched_shim_writes_its_bare_target_and_others_write_no_search_keys(tmp_path: Path) -> None:
    """A `search_path` shim's configuration keeps the program name and both search keys; a plain shim's text has neither, so
    the configuration of a shim installed before the option existed still matches its recorded hash."""
    searched = '[[shim]]\nname = "s"\ntarget = "python"\nsearch_path = true\nskip_app_execution_aliases = true\n'
    effects = {Path(str(item.fields["path"])).stem: item for item in desired_effects(manifest(SHIM + searched), context(tmp_path))[0] if item.kind == "shim"}

    searched_config = tomllib.loads(shim_config_text(effects["s"].fields))
    plain_text = shim_config_text(effects["t"].fields)

    assert (searched_config["target"], searched_config["search_path"], searched_config["skip_app_execution_aliases"]) == ("python", True, True)
    assert "search_path" not in plain_text and "skip_app_execution_aliases" not in plain_text
    assert "search_path" not in effects["t"].fields


def test_a_static_shim_needs_no_library_folder(tmp_path: Path, system: FakeSystem) -> None:
    """A static launcher links its runtime in: no ``exe-shim-lib`` folder is written for it."""
    ctx = replace(context(tmp_path), linkage="static")
    effect = next(item for item in desired_effects(manifest(SHIM), ctx)[0] if item.kind == "shim")

    apply_effect(system, effect, ctx)

    assert (tmp_path / "bin" / "t.exe").is_file() and not (tmp_path / "bin" / "exe-shim-lib").exists()


def test_verify_reports_a_missing_library_and_apply_puts_it_back(tmp_path: Path, system: FakeSystem) -> None:
    """A dynamic shim without its ``exe-shim-lib`` files is reported as drift, and applying the effect again restores them."""
    ctx = context(tmp_path)
    effect = next(item for item in desired_effects(manifest(SHIM), ctx)[0] if item.kind == "shim")
    applied = apply_effect(system, effect, ctx)
    (tmp_path / "bin" / "exe-shim-lib" / "libwinpthread-1.dll").unlink()

    drift = [row[1] for row in verify_each(system, [applied], mode=Mode.USER) if row[1]]

    assert drift and "libwinpthread-1.dll" in drift[0] and "exe-shim-lib" in drift[0]
    apply_effect(system, effect, ctx)
    assert [row for row in verify_each(system, [applied], mode=Mode.USER) if row[1] or row[2]] == []
