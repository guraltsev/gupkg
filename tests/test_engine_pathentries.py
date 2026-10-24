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
from gupkg.model.manifest import ManifestError, parse_manifest
from gupkg.model.state import Effect
from gupkg.system.fake import FakeSystem
from gupkg.system.port import Scope
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

HEAD = 'format = 2\nmodes = ["system", "user", "portable"]\n[source]\ndownload_url = "https://x.test/a.zip"\n'

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


@pytest.mark.parametrize("value", ["$App\\bin", "$App", "C:\\Tools\\bin", "${ProgramFiles}\\Tool", "%LOCALAPPDATA%\\Volta\\bin", "D:/Tools/x"])
def test_the_manifest_validator_accepts_ordinary_entries(value: str) -> None:
    """Variables, drive paths with either separator and a leading %VAR% are valid declarations."""
    toml_value = '"' + value.replace("\\", "\\\\") + '"'

    assert parse_manifest(HEAD + f"[[path]]\nvalue = {toml_value}\n").paths == (value,)


def context(tmp_path: Path, *, mode: str = "user", env: dict[str, str] | None = None) -> EffectContext:
    """An effect context below *tmp_path*: the user scope, or the machine scope for mode `system`."""
    package = tmp_path / "opt" / "tool"
    return EffectContext(
        package, mode, tmp_path / "bin", tmp_path / "menu", Scope.MACHINE if mode == "system" else Scope.USER, None,
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
    system.set_path(Scope.MACHINE, ["C:\\Windows"])

    for entry in (link, writable):
        with pytest.raises(EffectError):
            apply_effect(system, Effect("path", {"scope": "system", "entry": str(entry), "added": False}), ctx)

    assert system.get_path(Scope.MACHINE) == ["C:\\Windows"]
    fine = ctx.package_path / "fine"
    fine.mkdir()
    applied = apply_effect(system, Effect("path", {"scope": "system", "entry": str(fine), "added": False}), ctx)
    assert applied.fields["added"] is True and system.get_path(Scope.MACHINE) == ["C:\\Windows", str(fine)]


@pytest.mark.parametrize("entry", ["C:\\a;C:\\evil", "rel", "..\\x", "\\\\srv\\share", "C:\\Windows\\System32", "%LOCALAPPDATA%\\bin"])
def test_a_hostile_install_record_cannot_make_uninstall_touch_the_machine_path(tmp_path: Path, entry: str) -> None:
    """A recorded system PATH entry outside the trusted places (or of a bad shape) is refused on removal: PATH is unchanged."""
    system = FakeSystem(tmp_path / "machine")
    ctx = context(tmp_path, mode="system")
    before = ["C:\\Windows\\System32", "C:\\a", "C:\\evil", "C:\\Tools"]
    system.set_path(Scope.MACHINE, before)

    with pytest.raises(EffectError):
        remove_effect(system, Effect("path", {"scope": "system", "entry": entry, "added": True}), ctx)

    assert system.get_path(Scope.MACHINE) == before


def test_uninstall_removes_exactly_what_was_added_and_leaves_unrelated_entries(tmp_path: Path) -> None:
    """End to end in an elevated system install: the package's entry goes on PATH and off again; other entries stay in order."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system", tables='[[path]]\nvalue = "$App"\n[[path]]\nvalue = "$App\\\\bin"\n')
    world.fake.set_path(Scope.MACHINE, ["C:\\Windows", "C:\\Tools"])

    assert world.one("install", "system:tool").ok
    entries = world.fake.get_path(Scope.MACHINE)
    package = str(world.system_root / "tool" / "current" / "App")
    assert entries[:2] == ["C:\\Windows", "C:\\Tools"] and package in entries and package + "\\bin" in entries

    assert world.one("uninstall", "system:tool").ok
    assert world.fake.get_path(Scope.MACHINE) == ["C:\\Windows", "C:\\Tools"]


def test_a_package_with_a_hostile_path_is_not_installed(tmp_path: Path) -> None:
    """The validator refuses the package before anything is downloaded, in the elevated system mode too."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system", tables='[[path]]\nvalue = "C:\\\\a;C:\\\\evil"\n')
    world.fake.set_path(Scope.MACHINE, ["C:\\Windows"])
    world.fake.fetch_log.clear()

    result = world.one("install", "system:tool")

    assert not result.ok and world.fake.get_path(Scope.MACHINE) == ["C:\\Windows"] and world.fake.fetch_log == []
