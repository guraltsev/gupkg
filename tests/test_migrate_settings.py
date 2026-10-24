"""Cover ``gupkg-migrate settings``: a 0.17 manager configuration becomes a schema 3 settings file.

Behavior covered: roots, `bin` locations and the registry of a schema 2 `gupkg-config.toml`
come out of the settings loader (the backend of `gupkg settings show`) as the values the 0.17
file spells, with the registry named `main`; relative and `~` paths become
explicit; `--check` writes nothing; an existing settings file is kept unless `--force` is
given; a file that is not a schema 2 manager configuration is refused.

Real: the command line and the settings loader, on files under a temporary folder. Mocked: nothing (the run is not
elevated, so the admin-only folder check is not involved). Out of scope: the 0.17 registry
cache and shim linkage, which schema 3 drops.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from gupkg.settingsload import load_settings, settings_file
from gupkg.system.fake import FakeSystem
from gupkg_migrate.cli import main


def same(left: str | Path, right: str | Path) -> bool:
    """Return whether two paths name the same place, ignoring case and separators."""
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def write_config(folder: Path, system: str, user: str, bin_system: str, bin_user: str, extra: str = "") -> Path:
    """Write a schema 2 manager configuration with literal TOML string values."""
    path = folder / "gupkg-config.toml"
    path.write_text(
        'mode = "manager"\nschema_version = 2\n'
        f"[packages]\nsystem = '{system}'\nuser = '{user}'\n[bin]\nsystem = '{bin_system}'\nuser = '{bin_user}'\n" + extra,
        encoding="utf-8",
    )
    return path


@pytest.fixture
def machine(tmp_path: Path) -> dict:
    """A 0.17 configuration over roots in a temporary folder and the environment of a fake user."""
    home = tmp_path / "home"
    config = write_config(
        tmp_path, str(tmp_path / "opt"), str(home / "opt"), str(tmp_path / "bin"), str(home / "bin"),
        f"[registry]\ncache = '{tmp_path / 'cache'}'\nsource = 'https://example.org/team-registry.git'\n"
        '[shims]\nlinkage = "static"\n',
    )
    return {"config": config, "system": tmp_path / "opt", "env": {"USERPROFILE": str(home)}}


def load(machine: dict):
    """Load the settings of a normal run on the fake machine."""
    return load_settings(
        system=FakeSystem(machine["system"].parent / "fake-machine"), env=machine["env"], elevated=False, system_mode=False, app_dir=None, installed_system_root=machine["system"]
    )


def test_converted_settings_show_the_same_roots_bin_locations_and_registry(machine: dict, capsys: pytest.CaptureFixture[str]) -> None:
    """The settings loader reports the roots, bin directories and registry that the 0.17 loader read, the registry named `main`."""
    home = Path(machine["env"]["USERPROFILE"])
    assert main(["settings", str(machine["config"])]) == 0

    written = settings_file(machine["system"])
    assert written.is_file() and str(written) in capsys.readouterr().out
    settings = load(machine).settings
    assert same(settings["roots.system"], machine["system"])
    assert same(settings["roots.user"], home / "opt")
    assert same(settings["integration.system.bin"], machine["system"].parent / "bin")
    assert same(settings["integration.user.bin"], home / "bin")
    assert [(entry.name, entry.source) for entry in settings.registries] == [("main", "https://example.org/team-registry.git")]
    assert settings.source_of("registry") == "system"
    assert settings.source_of("roots.system") == "system"


def test_notes_name_what_schema_3_drops(machine: dict, capsys: pytest.CaptureFixture[str]) -> None:
    """The registry cache folder and the shim linkage have no place in schema 3, and the run says so."""
    assert main(["settings", str(machine["config"])]) == 0

    output = capsys.readouterr().out
    assert "[registry].cache was dropped" in output
    assert "[shims].linkage = 'static' was dropped" in output


def test_relative_tilde_and_variable_paths_become_explicit(tmp_path: Path) -> None:
    """A path relative to the configuration file becomes absolute, `~` becomes `%USERPROFILE%`, `%VAR%` is kept."""
    config = write_config(tmp_path, "rel\\opt", "~/opt", "%SYSTEMDRIVE%\\bin", "~/bin")
    output = tmp_path / "settings.toml"

    assert main(["settings", str(config), "--output", str(output)]) == 0

    data = tomllib.loads(output.read_text(encoding="utf-8"))
    assert same(data["roots"]["system"], tmp_path / "rel" / "opt")
    assert data["roots"]["user"] == "%USERPROFILE%\\opt"
    assert data["integration"]["system"]["bin"] == "%SYSTEMDRIVE%\\bin"
    assert data["integration"]["user"]["bin"] == "%USERPROFILE%\\bin"
    assert data["schema"] == 3 and "registry" not in data


def test_check_mode_prints_the_result_and_writes_nothing(machine: dict, capsys: pytest.CaptureFixture[str]) -> None:
    """`--check` prints the settings text and where it would go, and creates no file."""
    assert main(["settings", str(machine["config"]), "--check"]) == 0

    output = capsys.readouterr().out
    assert "[would write]" in output and "schema = 3" in output and 'name = "main"' in output
    assert not settings_file(machine["system"]).exists()


def test_an_existing_settings_file_is_kept_unless_forced(machine: dict, capsys: pytest.CaptureFixture[str]) -> None:
    """The tool refuses to replace a settings file that exists, and replaces it with `--force`."""
    target = settings_file(machine["system"])
    target.parent.mkdir(parents=True)
    target.write_text('schema = 3\n[ui]\ncolor = "never"\n', encoding="utf-8")

    assert main(["settings", str(machine["config"])]) == 1
    assert 'color = "never"' in target.read_text(encoding="utf-8")
    assert "already exists" in capsys.readouterr().err

    assert main(["settings", str(machine["config"]), "--force"]) == 0
    assert 'name = "main"' in target.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('schema = 3\n[ui]\ncolor = "auto"\n', id="already-schema-3"),
        pytest.param('mode = "package"\nschema_version = 2\n', id="not-a-manager-config"),
        pytest.param("not toml [", id="not-toml"),
    ],
)
def test_other_files_are_refused(tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Anything but a schema 2 manager configuration is refused with an error and no output file."""
    config = tmp_path / "gupkg-config.toml"
    config.write_text(text, encoding="utf-8")

    assert main(["settings", str(config), "--output", str(tmp_path / "out.toml")]) == 1

    assert not (tmp_path / "out.toml").exists()
    assert "[error]" in capsys.readouterr().err
