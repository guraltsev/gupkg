"""Cover where the manager configuration may be read from.

Behavior covered: the configuration is taken only from the gupkg package
directory or an explicit ``--config``; files in ``%APPDATA%``, ``GUPKG_HOME`` and
the working directory are ignored; and an elevated run refuses a configuration
whose folder non-administrators can modify.

Real: the command line, configuration loading, and (on Windows) the access-control
check of a temporary folder. Mocked: the package directory location and the
process's elevation state. Out of scope: what the configuration then does.
"""

from __future__ import annotations

import io
import os
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from gupkg import cli, commands
from gupkg.core import ConfigValidationError


def _config_text(root: Path) -> str:
    """Return a valid manager configuration whose folders live below *root*."""
    return (
        'mode = "manager"\nschema_version = 2\n[packages]\n'
        f"system = '{root / 'system'}'\nuser = '{root / 'user'}'\n"
        f"[bin]\nsystem = '{root / 'bin-system'}'\nuser = '{root / 'bin-user'}'\n"
    )


def test_configuration_in_user_writable_locations_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config in %APPDATA%, GUPKG_HOME or the working directory is never used."""
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.setattr("gupkg.manager.__file__", str(package_dir / "manager.py"))
    roaming, home, cwd = tmp_path / "roaming", tmp_path / "home", tmp_path / "cwd"
    for folder in (roaming / "gupkg", home, cwd):
        folder.mkdir(parents=True)
        (folder / "gupkg-config.toml").write_text(_config_text(tmp_path), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("GUPKG_HOME", str(home))
    monkeypatch.chdir(cwd)

    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(["--format", "toml", "manager", "list"])

    assert code == 2
    assert "No manager configuration found" in tomllib.loads(output.getvalue())["errors"][0]


def test_configuration_in_the_package_directory_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The configuration beside the gupkg package is found without any option."""
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "gupkg-config.toml").write_text(_config_text(tmp_path), encoding="utf-8")
    monkeypatch.setattr("gupkg.manager.__file__", str(package_dir / "manager.py"))

    assert commands.load_manager().path == package_dir / "gupkg-config.toml"


@pytest.mark.skipif(os.name != "nt", reason="reads Windows access-control lists")
def test_elevated_run_refuses_a_configuration_in_a_user_writable_folder(tmp_path: Path) -> None:
    """Even an explicit --config is refused under elevation when its folder is not admin-only."""
    config = tmp_path / "gupkg-config.toml"
    config.write_text(_config_text(tmp_path), encoding="utf-8")

    with mock.patch.object(commands, "elevated_mode", return_value=True):
        with pytest.raises(ConfigValidationError, match="Refusing the manager configuration"):
            commands.load_manager(config)

    with mock.patch.object(commands, "elevated_mode", return_value=False):
        assert commands.load_manager(config).path == config
