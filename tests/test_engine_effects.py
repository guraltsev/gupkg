"""Cover the integration effects: how declarations become effects, and how effects are applied, removed and verified.

Behaviour covered: variable expansion (``$App``, ``$PackageRoot``, ``$Bin``, ``$$``, ``${NAME}``) and its errors; shortcuts and
shims confined to the Start Menu and ``bin`` folders; a mode without a Start Menu or an environment target leaves those
rows out and says so; ``[portable]`` arguments and environment; apply and remove of every kind through ``FakeSystem``;
a hostile install record cannot make gupkg delete or write outside the mode's folders; a ``PATH`` entry is removed only when this install
added it and never when it is the shared ``bin`` folder; verification reads only what gupkg wrote and skips the user's
environment in an elevated run.

Real: the effect code and real folders. Mocked: ``FakeSystem`` (registry, shortcuts). Out of scope: the install flow around it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from gupkg.engine.effects import (
    EffectContext,
    EffectError,
    apply_effect,
    desired_effects,
    remove_effect,
    verify_each,
)
from gupkg.model.manifest import EnvVar, parse_manifest
from gupkg.model.state import Effect
from gupkg.system.fake import FakeSystem
from gupkg.system.port import Scope


@pytest.fixture
def system(tmp_path: Path) -> FakeSystem:
    """A fake machine whose files live below the temporary folder."""
    return FakeSystem(tmp_path / "machine")


def context(tmp_path: Path, *, mode: str = "user", start_menu: bool = True, scope: Scope | None = Scope.USER, env=None) -> EffectContext:
    """Return an effect context below *tmp_path*."""
    package = tmp_path / "opt" / "tool"
    return EffectContext(
        package, mode, tmp_path / "bin", tmp_path / "menu" if start_menu else None, scope,
        package / "data" if mode == "portable" else None, env or {"USERPROFILE": "C:\\Users\\me"},
    )


def manifest(text: str):
    """Parse a version manifest with a download source and *text* after it."""
    return parse_manifest('format = 2\nmodes = ["system", "user", "portable"]\n[source]\ndownload_url = "https://x.test/a.zip"\n' + text)


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
        pytest.param('[[env]]\nname = "A"\nvalue = "$Data"\n', "portable mode", id="data-outside-portable"),
        pytest.param('[[shortcut]]\nname = "Tool:ads"\ntarget = "$App\\\\a.exe"\n', "stream", id="shortcut-ntfs-stream"),
        pytest.param('[[shortcut]]\nname = "NUL"\ntarget = "$App\\\\a.exe"\n', "device name", id="shortcut-nul"),
        pytest.param('[[shortcut]]\nname = "aux.lnk"\ntarget = "$App\\\\a.exe"\n', "device name", id="shortcut-aux-lnk"),
        pytest.param('[[shortcut]]\nname = "COM1"\ntarget = "$App\\\\a.exe"\n', "device name", id="shortcut-com1"),
        pytest.param('[[shortcut]]\nname = "Group\\\\con"\ntarget = "$App\\\\a.exe"\n', "device name", id="shortcut-device-in-folder"),
    ],
)
def test_a_declaration_that_cannot_become_an_effect_is_refused(tmp_path: Path, row: str, message: str) -> None:
    """Unknown references, a name that leaves the Start Menu and `$Data` outside portable mode are errors, not guesses."""
    with pytest.raises(EffectError, match=message):
        desired_effects(manifest(row), context(tmp_path))


def test_a_mode_without_a_start_menu_or_an_environment_target_leaves_those_rows_out(tmp_path: Path) -> None:
    """Portable `integrate = "none"`: shims stay, shortcuts, environment values and PATH entries are skipped and reported."""
    text = (
        '[[shortcut]]\nname = "T"\ntarget = "$App\\\\a.exe"\n[[shim]]\nname = "t"\ntarget = "$App\\\\a.exe"\n'
        '[[env]]\nname = "A"\nvalue = "1"\n[[path]]\nvalue = "$App"\n'
    )

    effects, notes = desired_effects(manifest(text), context(tmp_path, mode="portable", start_menu=False, scope=None))

    assert [effect.kind for effect in effects] == ["shim"] and len(notes) == 3
    assert any("gupkg shell" in note for note in notes)


def test_portable_arguments_and_environment_apply_only_in_portable_mode(tmp_path: Path) -> None:
    """`[portable] args` are appended to shortcuts and shims and `[portable] env` becomes environment values, in portable mode only."""
    text = (
        '[[shortcut]]\nname = "T"\ntarget = "$App\\\\a.exe"\narguments = "--x"\n[[shim]]\nname = "t"\ntarget = "$App\\\\a.exe"\n'
        '[portable]\nargs = ["--portable", "$Data\\\\p"]\nenv = { TOOL_DATA = "$Data" }\n'
    )

    portable, _ = desired_effects(manifest(text), context(tmp_path, mode="portable"))
    plain, _ = desired_effects(manifest(text), context(tmp_path))

    data = str(tmp_path / "opt" / "tool" / "data")
    by_kind = {effect.kind: effect for effect in portable if effect.kind != "path"}
    assert by_kind["shortcut"].fields["arguments"] == f'--x --portable {data}\\p'
    assert by_kind["shim"].fields["arguments"] == ["--portable", f"{data}\\p"]
    assert by_kind["env"].fields == {"scope": "user", "name": "TOOL_DATA", "value": data}
    assert [effect.fields.get("arguments") for effect in plain if effect.kind in ("shortcut", "shim")] == ["--x", []]
    assert "env" not in {effect.kind for effect in plain}


def test_two_declarations_of_the_same_thing_are_an_error(tmp_path: Path) -> None:
    """A shortcut declared twice with different targets would silently lose one: the package is refused."""
    text = '[[shortcut]]\nname = "T"\ntarget = "$App\\\\a.exe"\n[[shortcut]]\nname = "t"\ntarget = "$App\\\\b.exe"\n'

    with pytest.raises(EffectError, match="same shortcut"):
        desired_effects(manifest(text), context(tmp_path))


def test_every_kind_is_applied_verified_and_removed(tmp_path: Path, system: FakeSystem) -> None:
    """apply creates the shortcut, the shim files, the environment value and the PATH entry; verify finds them; remove deletes them."""
    ctx = context(tmp_path)
    text = (
        '[[shortcut]]\nname = "Group\\\\T"\ntarget = "$App\\\\a.exe"\n[[shim]]\nname = "t"\ntarget = "$App\\\\a.exe"\n'
        '[[shim]]\nname = "run.cmd"\ncontent = "@echo off\\r\\necho %~dp0\\r\\n"\n'
        '[[env]]\nname = "A"\nvalue = "1"\n[[path]]\nvalue = "$App"\n'
    )
    effects, _ = desired_effects(manifest(text), ctx)

    applied = [apply_effect(system, effect, ctx) for effect in effects]

    assert (tmp_path / "bin" / "t.exe").is_file() and (tmp_path / "bin" / "t.config.toml").is_file()
    assert (tmp_path / "bin" / "run.cmd").read_bytes() == b"@echo off\r\necho %~dp0\r\n"
    assert [item for item in verify_each(system, applied, elevated=False) if item[1] or item[2]] == []
    assert next(item for item in applied if item.kind == "path").fields["added"] is True
    removed = {(effect.kind, bool(effect.fields.get("shared"))): remove_effect(system, effect, ctx) for effect in applied}
    assert all(done for (kind, shared), done in removed.items() if not shared) and removed[("path", True)] is False
    assert not (tmp_path / "bin" / "t.exe").exists() and not (tmp_path / "menu" / "Group").exists()
    assert system.get_path(Scope.USER) == [str(tmp_path / "bin")] and "A" not in system.env_values(Scope.USER)
    assert [item[0].kind for item in verify_each(system, applied, elevated=False) if item[1]] == ["shortcut", "shim", "file", "env", "path"]


def test_a_path_entry_that_was_already_there_is_never_removed(tmp_path: Path, system: FakeSystem) -> None:
    """Only an entry this install added is removed; one that was on PATH before stays when the package goes."""
    ctx = context(tmp_path)
    entry = str(tmp_path / "opt" / "tool" / "current" / "App")
    system.set_path(Scope.USER, [entry])
    [effect], _ = desired_effects(manifest('[[path]]\nvalue = "$App"\n'), ctx)

    applied = apply_effect(system, effect, ctx)

    assert applied.fields["added"] is False
    assert remove_effect(system, applied, ctx) is False and system.get_path(Scope.USER) == [entry]


def test_the_shared_bin_entry_is_never_removed_by_one_package(tmp_path: Path, system: FakeSystem) -> None:
    """The bin folder belongs to the mode and other packages' shims need it: removing one package leaves it on PATH."""
    ctx = context(tmp_path)
    effects, _ = desired_effects(manifest('[[shim]]\nname = "t"\ntarget = "$App\\\\a.exe"\n'), ctx)
    applied = [apply_effect(system, effect, ctx) for effect in effects]
    entry = next(item for item in applied if item.kind == "path")
    assert entry.fields["added"] is True and entry.fields["shared"] is True

    assert remove_effect(system, entry, ctx) is False and str(tmp_path / "bin") in system.get_path(Scope.USER)


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
    """Verifying an installed package reads the values and the PATH of its record and nothing else; an elevated run skips the user's."""
    ctx = context(tmp_path)
    effects, _ = desired_effects(manifest('[[env]]\nname = "A"\nvalue = "1"\n[[path]]\nvalue = "$App"\n'), ctx)
    applied = [apply_effect(system, effect, ctx) for effect in effects]
    system.registry_log.clear()

    verify_each(system, applied, elevated=False)
    assert {(item.scope, item.name) for item in system.registry_log if item.operation == "read"} == {("user", "A"), ("user", "Path")}
    system.registry_log.clear()
    rows = verify_each(system, applied, elevated=True)

    assert system.registry_log == [] and all(unverified and not drift for _, drift, unverified in rows)


@pytest.mark.parametrize("name", ["Path", "PATH", "TEMP", "ComSpec"])
def test_a_reserved_variable_is_never_set_or_deleted_by_an_effect(tmp_path: Path, system: FakeSystem, name: str) -> None:
    """Defence in depth: a recorded `env` effect named like a system variable is refused on apply and on removal, and PATH is untouched."""
    ctx = context(tmp_path)
    system.set_path(Scope.USER, ["C:\\A", "C:\\B"])
    effect = Effect("env", {"scope": "user", "name": name, "value": "C:\\only"})

    with pytest.raises(EffectError, match="reserved"):
        apply_effect(system, effect, ctx)
    with pytest.raises(EffectError, match="reserved"):
        remove_effect(system, effect, ctx)

    assert system.get_path(Scope.USER) == ["C:\\A", "C:\\B"]


def test_a_manifest_with_a_reserved_variable_cannot_become_effects(tmp_path: Path) -> None:
    """A manifest built without the parser (a hostile version directory) is still refused when the effects are derived."""
    ctx = context(tmp_path)
    parsed = manifest('[[env]]\nname = "A"\nvalue = "1"\n')
    hostile = replace(parsed, envs=(EnvVar("Path", "C:\\only"),))

    with pytest.raises(EffectError, match="reserved"):
        desired_effects(hostile, ctx)
