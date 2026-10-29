"""Cover what may become a `PATH` entry: one absolute local folder, and in the machine `PATH` only a trusted one.

Behaviour covered: the manifest validator and `desired_effects` refuse `..\\x`, a bare `rel`, `\\\\srv\\share`, `\\\\?\\C:\\x`,
`$App\\..\\..` (it resolves outside the package), `C:\\a;C:\\evil` (one declaration, two entries), quotes and control
characters, device names; a `%VAR%\\...` prefix is allowed for a user `PATH` only; the machine `PATH` takes only a path inside the
package, the `bin` folder or a machine-wide folder (`ProgramFiles`, `SystemRoot`); an existing folder that is a junction or that standard
users can write is refused when the entry is applied; a hostile install record cannot make `remove_effect` touch anything; uninstall
removes exactly the entries that were added and leaves the unrelated ones.

Real: the effect code, real folders and junctions. Mocked: `FakeSystem` (registry `PATH`, admin-only folders).
Out of scope: the shared `bin` entry (see test_engine_effects).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from gupkg.engine.effects import EffectContext, EffectError, apply_effect, desired_effects, remove_effect
from gupkg.model.manifest import ManifestError, parse_manifest, path_entry_shape_problem
from gupkg.model.state import Effect
from gupkg.system.fake import FakeSystem
from gupkg.system.port import Mode
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

HEAD = 'format = 2\nmodes = ["system", "user"]\n[source]\ndownload_url = "https://x.test/a.zip"\n'

BAD = [
    pytest.param("..\\x", id="parent"),
    pytest.param("rel", id="bare-relative"),
    pytest.param("rel\\bin", id="relative-with-folder"),
    pytest.param("C:rel", id="drive-relative"),
    pytest.param("\\\\srv\\share", id="unc"),
    pytest.param("//srv/share", id="unc-slashes"),
    pytest.param("\\\\?\\C:\\x", id="extended-length"),
    pytest.param("\\\\.\\C:\\x", id="device-path"),
    pytest.param("$App\\..\\..", id="escapes-the-package"),
    pytest.param("C:\\a;C:\\evil", id="two-entries"),
    pytest.param('C:\\a"b', id="quote"),
    pytest.param("C:\\a\x07b", id="control-character"),
    pytest.param("C:\\nul", id="device-name"),
    pytest.param("C:\\a\\..\\b", id="parent-in-the-middle"),
    pytest.param("%A%\\%B%\\x", id="second-percent"),
    pytest.param("C:\\50%\\x", id="percent-in-the-middle"),
]


@pytest.mark.parametrize("value", BAD)
def test_the_manifest_validator_refuses_a_path_entry_that_is_not_one_absolute_folder(value: str) -> None:
    """The declaration itself is invalid, so no tool can install such a package."""
    toml_value = '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\x07", "\\u0007") + '"'
    text = HEAD + f"[[path]]\nvalue = {toml_value}\n"

    with pytest.raises(ManifestError, match="path\\[0\\].value"):
        parse_manifest(text)


@pytest.mark.parametrize("value", ["$App\\bin", "$App", "C:\\Tools\\bin", "${ProgramFiles}\\Tool", "D:/Tools/x"])
def test_the_manifest_validator_accepts_ordinary_entries(value: str) -> None:
    """Variables and drive paths with either separator are valid declarations, in a package that supports system mode too."""
    toml_value = '"' + value.replace("\\", "\\\\") + '"'

    assert parse_manifest(HEAD + f"[[path]]\nvalue = {toml_value}\n").paths == (value,)


@pytest.mark.parametrize("modes", ['["system", "user"]', '["system"]'])
def test_an_entry_that_follows_an_environment_variable_fails_inspection_when_system_mode_is_supported(modes: str) -> None:
    """`%LOCALAPPDATA%\\Volta\\bin` can never go on the machine PATH, so a package that lists 'system' and declares it is invalid."""
    text = f'format = 2\nmodes = {modes}\n[source]\ndownload_url = "https://x.test/a.zip"\n[[path]]\nvalue = "%LOCALAPPDATA%\\\\Volta\\\\bin"\n'

    with pytest.raises(ManifestError, match=r"path\[0\].value.*environment variable.*machine PATH"):
        parse_manifest(text)


@pytest.mark.parametrize("modes", ['["user"]'])
def test_a_package_without_system_mode_may_declare_an_entry_that_follows_an_environment_variable(modes: str) -> None:
    """A user PATH is the user's own, so `%LOCALAPPDATA%\\Volta\\bin` is a valid entry when system mode is not supported."""
    text = f'format = 2\nmodes = {modes}\n[source]\ndownload_url = "https://x.test/a.zip"\n[[path]]\nvalue = "%LOCALAPPDATA%\\\\Volta\\\\bin"\n'

    assert parse_manifest(text).paths == ("%LOCALAPPDATA%\\Volta\\bin",)


def context(tmp_path: Path, *, mode: str = "user", env: dict[str, str] | None = None) -> EffectContext:
    """An effect context below *tmp_path*: the user scope, or the machine scope for mode `system`."""
    package = tmp_path / "opt" / "tool"
    return EffectContext(
        package, mode, tmp_path / "bin", tmp_path / "menu", Mode.SYSTEM if mode == "system" else Mode.USER,
        env if env is not None else {"ProgramFiles": str(tmp_path / "pf"), "SystemRoot": str(tmp_path / "win"), "LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"},
    )


def with_paths(*paths: str):
    """A manifest built without the validator (a hostile version directory), holding *paths*."""
    return replace(parse_manifest(HEAD), paths=tuple(paths))


@pytest.mark.parametrize("mode", ["user", "system"])
@pytest.mark.parametrize("value", [
    "..\\x", "rel", "\\\\srv\\share", "\\\\?\\C:\\x", "$App\\..\\..", "C:\\a;C:\\evil", 'C:\\a"b', "C:\\nul", "$App\\..\\..\\evil",
])
def test_desired_effects_refuses_the_same_shapes_after_expansion(tmp_path: Path, mode: str, value: str) -> None:
    """A manifest that skipped the validator still cannot produce such an entry; `$App\\..\\..` is judged after it is expanded."""
    with pytest.raises(EffectError):
        desired_effects(with_paths(value), context(tmp_path, mode=mode))


def test_the_machine_path_takes_only_trusted_places(tmp_path: Path) -> None:
    """System mode: the package, the bin folder and machine-wide folders pass; a user's folder or a %VAR% prefix does not."""
    ctx = context(tmp_path, mode="system")
    inside = str(ctx.package_path / "current" / "App")
    program_files = str(tmp_path / "pf" / "Tool")
    system_root = str(tmp_path / "win" / "System32")
    for folder in (program_files, system_root, ctx.bin_dir / "x"):
        Path(folder).mkdir(parents=True)

    effects, _ = desired_effects(with_paths(inside, program_files, system_root, str(ctx.bin_dir / "x")), ctx)

    assert [effect.fields["entry"] for effect in effects if effect.kind == "path"] == [inside, program_files, system_root, str(ctx.bin_dir / "x")]
    for hostile in ("C:\\Users\\Public\\bin", "C:\\Temp", str(tmp_path / "pf-other"), "%LOCALAPPDATA%\\bin", "%ProgramFiles%\\Tool", "$App\\..\\x"):
        with pytest.raises(EffectError):
            desired_effects(with_paths(hostile), ctx)


def test_the_user_path_takes_any_absolute_local_folder_and_a_leading_variable(tmp_path: Path) -> None:
    """User mode is the user's own PATH: a folder anywhere on a drive and `%LOCALAPPDATA%\\...` are fine."""
    ctx = context(tmp_path, mode="user")

    effects, _ = desired_effects(with_paths("D:\\Tools\\bin", "%LOCALAPPDATA%\\Volta\\bin"), ctx)

    assert [effect.fields["entry"] for effect in effects] == ["D:\\Tools\\bin", "%LOCALAPPDATA%\\Volta\\bin"]


def test_an_existing_junction_or_user_writable_folder_cannot_go_on_the_machine_path(tmp_path: Path) -> None:
    """Applying a machine PATH entry checks the folder when it exists: links and folders standard users can change are refused."""
    system = FakeSystem(tmp_path / "machine")
    ctx = context(tmp_path, mode="system")
    target = tmp_path / "elsewhere"
    target.mkdir()
    ctx.package_path.mkdir(parents=True)
    link = ctx.package_path / "link"
    system.create_junction(link, target)
    writable = ctx.package_path / "writable"
    writable.mkdir()
    system.untrusted_dirs = {Path(str(writable))}
    system.set_path(Mode.SYSTEM, ["C:\\Windows"])

    for entry in (link, writable):
        with pytest.raises(EffectError):
            apply_effect(system, Effect("path", {"scope": "system", "entry": str(entry), "added": False}), ctx)

    assert system.get_path(Mode.SYSTEM) == ["C:\\Windows"]
    fine = ctx.package_path / "fine"
    fine.mkdir()
    applied = apply_effect(system, Effect("path", {"scope": "system", "entry": str(fine), "added": False}), ctx)
    assert applied.fields["added"] is True and system.get_path(Mode.SYSTEM) == ["C:\\Windows", str(fine)]


@pytest.mark.parametrize("entry", ["C:\\a;C:\\evil", "rel", "..\\x", "\\\\srv\\share", "C:\\Windows\\System32", "%LOCALAPPDATA%\\bin"])
def test_a_hostile_install_record_cannot_make_uninstall_touch_the_machine_path(tmp_path: Path, entry: str) -> None:
    """A recorded system PATH entry outside the trusted places (or of a bad shape) is refused on removal: PATH is unchanged."""
    system = FakeSystem(tmp_path / "machine")
    ctx = context(tmp_path, mode="system")
    before = ["C:\\Windows\\System32", "C:\\a", "C:\\evil", "C:\\Tools"]
    system.set_path(Mode.SYSTEM, before)

    with pytest.raises(EffectError):
        remove_effect(system, Effect("path", {"scope": "system", "entry": entry, "added": True}), ctx)

    assert system.get_path(Mode.SYSTEM) == before


def test_uninstall_removes_exactly_what_was_added_and_leaves_unrelated_entries(tmp_path: Path) -> None:
    """End to end in an elevated system install: the package's entry goes on PATH and off again; other entries stay in order."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system", tables='[[path]]\nvalue = "$App"\n[[path]]\nvalue = "$App\\\\bin"\n')
    world.fake.set_path(Mode.SYSTEM, ["C:\\Windows", "C:\\Tools"])

    assert world.one("install", "system:tool").ok
    entries = world.fake.get_path(Mode.SYSTEM)
    package = str(world.system_root / "tool" / "current" / "App")
    assert entries[:2] == ["C:\\Windows", "C:\\Tools"] and package in entries and package + "\\bin" in entries

    assert world.one("uninstall", "system:tool").ok
    assert world.fake.get_path(Mode.SYSTEM) == ["C:\\Windows", "C:\\Tools"]


def test_a_package_with_a_hostile_path_is_not_installed(tmp_path: Path) -> None:
    """The validator refuses the package before anything is downloaded, in the elevated system mode too."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system", tables='[[path]]\nvalue = "C:\\\\a;C:\\\\evil"\n')
    world.fake.set_path(Mode.SYSTEM, ["C:\\Windows"])
    world.fake.fetch_log.clear()

    result = world.one("install", "system:tool")

    assert not result.ok and world.fake.get_path(Mode.SYSTEM) == ["C:\\Windows"] and world.fake.fetch_log == []


def machine(tmp_path: Path):
    """A system context with an untrusted `<SystemRoot>\\Temp`, a package with a real `current\\App`, and a fake machine that knows both."""
    ctx = context(tmp_path, mode="system")
    temp = tmp_path / "win" / "Temp"
    temp.mkdir(parents=True)
    (ctx.package_path / "current" / "App").mkdir(parents=True)
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={temp})
    system.set_path(Mode.SYSTEM, ["C:\\Windows"])
    return ctx, system, temp


def system_effect(entry: Path | str) -> Effect:
    """A machine PATH effect for *entry*."""
    return Effect("path", {"scope": "system", "entry": str(entry), "added": False})


@pytest.mark.parametrize("relative", ["", "x", "x\\y"])
def test_a_machine_path_entry_below_an_untrusted_machine_folder_is_refused_even_when_it_does_not_exist_yet(tmp_path: Path, relative: str) -> None:
    """`<SystemRoot>\\Temp`, `\\x` and `\\x\\y`: a standard user could create the missing folder later, so none goes on the machine PATH."""
    ctx, system, temp = machine(tmp_path)
    entry = temp / relative if relative else temp

    with pytest.raises(EffectError):
        apply_effect(system, system_effect(entry), ctx)
    if relative:
        with pytest.raises(EffectError):
            desired_effects(with_paths(str(entry)), ctx)  # planning refuses a missing folder outside the package; an existing one is judged on apply

    assert system.get_path(Mode.SYSTEM) == ["C:\\Windows"]


def test_a_missing_folder_below_a_junction_inside_the_package_is_refused(tmp_path: Path) -> None:
    """`current\\App\\t` is a junction to `<SystemRoot>\\Temp` and `x` does not exist: the entry is judged through its nearest existing ancestor."""
    ctx, system, temp = machine(tmp_path)
    system.create_junction(ctx.package_path / "current" / "App" / "t", temp)

    for entry in (ctx.package_path / "current" / "App" / "t" / "x", ctx.package_path / "current" / "App" / "t" / "x" / "y", ctx.package_path / "current" / "App" / "t"):
        with pytest.raises(EffectError, match="junction|link|outside the package"):
            apply_effect(system, system_effect(entry), ctx)

    assert system.get_path(Mode.SYSTEM) == ["C:\\Windows"]


def test_a_missing_folder_below_an_untrusted_folder_inside_the_package_is_refused(tmp_path: Path) -> None:
    """The nearest existing ancestor must be admin-only: a user-writable subfolder of the package does not pass."""
    ctx, system, _ = machine(tmp_path)
    writable = ctx.package_path / "current" / "App" / "writable"
    writable.mkdir()
    system.untrusted_dirs = {Path(str(writable))}

    with pytest.raises(EffectError, match="standard users"):
        apply_effect(system, system_effect(writable / "later"), ctx)


def test_the_bin_folder_variant_is_refused_when_the_bin_folder_is_untrusted(tmp_path: Path) -> None:
    """`<bin>\\x` with an untrusted bin folder is refused whether `x` exists or not."""
    ctx, system, _ = machine(tmp_path)
    ctx.bin_dir.mkdir(parents=True)
    system.untrusted_dirs = {Path(str(ctx.bin_dir))}
    (ctx.bin_dir / "there").mkdir()

    for entry in (ctx.bin_dir / "there", ctx.bin_dir / "x", ctx.bin_dir / "x" / "y"):
        with pytest.raises(EffectError):
            apply_effect(system, system_effect(entry), ctx)


def test_a_not_yet_created_app_bin_in_an_admin_only_package_is_accepted(tmp_path: Path) -> None:
    """The package's own `$App\\bin` may be created later by the install itself: its existing ancestor `App` is admin-only, so it passes."""
    ctx, system, _ = machine(tmp_path)
    entry = ctx.package_path / "current" / "App" / "bin"

    effects, _ = desired_effects(with_paths("$App\\bin"), ctx)
    applied = apply_effect(system, system_effect(entry), ctx)

    assert [effect.fields["entry"] for effect in effects] == [str(entry)]
    assert applied.fields["added"] is True and system.get_path(Mode.SYSTEM) == ["C:\\Windows", str(entry)]


def test_removal_can_delete_a_recorded_entry_whose_folder_is_gone(tmp_path: Path) -> None:
    """The existence and ancestor rules are for applying; an entry that was added and whose folder vanished still comes off the PATH."""
    ctx, system, _ = machine(tmp_path)
    gone = ctx.package_path / "current" / "App" / "bin"
    system.set_path(Mode.SYSTEM, ["C:\\Windows", str(gone)])

    assert remove_effect(system, Effect("path", {"scope": "system", "entry": str(gone), "added": True}), ctx) is True

    assert system.get_path(Mode.SYSTEM) == ["C:\\Windows"]


def test_apply_effect_judges_a_hostile_effect_itself(tmp_path: Path) -> None:
    """`apply_effect` has its own check: a hand-made effect with a bad shape is refused although nothing planned it."""
    ctx, system, _ = machine(tmp_path)

    for entry in ("C:\\a;C:\\evil", "rel", "\\\\srv\\share", "C:\\x\\..\\y"):
        for scope in ("user", "system"):
            with pytest.raises(EffectError):
                apply_effect(system, Effect("path", {"scope": scope, "entry": entry, "added": False}), ctx)

    assert system.get_path(Mode.SYSTEM) == ["C:\\Windows"] and system.get_path(Mode.USER) == []


def test_user_mode_is_unchanged_by_the_machine_rules(tmp_path: Path) -> None:
    """A user PATH entry may be a folder that does not exist yet and may be anywhere on a drive."""
    ctx = context(tmp_path, mode="user")
    system = FakeSystem(tmp_path / "machine")

    applied = apply_effect(system, Effect("path", {"scope": "user", "entry": str(tmp_path / "later" / "bin"), "added": False}), ctx)

    assert applied.fields["added"] is True and system.get_path(Mode.USER) == [str(tmp_path / "later" / "bin")]


INVISIBLE = ["\x85", "\x7f", "\u200b", "\u202e", "\u2028", "\u2029", "\ufeff", "\ue000", "\ud800", "\u0378"]


@pytest.mark.parametrize("char", INVISIBLE, ids=lambda char: f"U+{ord(char):04X}")
def test_a_path_entry_with_a_control_invisible_or_separator_character_is_refused_everywhere(tmp_path: Path, char: str) -> None:
    """The whole Unicode category C* (and the line and paragraph separators): NEL, DEL, zero-width, bidirectional, lone surrogate..."""
    entry = f"C:\\tools{char}\\bin"
    ctx = context(tmp_path, mode="user")

    assert path_entry_shape_problem(entry, template=False) and path_entry_shape_problem(entry, template=True)
    with pytest.raises(EffectError):
        desired_effects(with_paths(entry), ctx)
    with pytest.raises(EffectError):
        apply_effect(FakeSystem(tmp_path / "machine"), Effect("path", {"scope": "user", "entry": entry, "added": False}), ctx)
    if char != "\ud800":
        with pytest.raises(ManifestError, match="path\\[0\\].value"):
            parse_manifest(HEAD + f'[[path]]\nvalue = "C:\\\\tools\\u{ord(char):04x}\\\\bin"\n')
