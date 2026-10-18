"""Cover which registry cache locations an elevated run accepts.

Behavior covered: an elevated run refuses to read or sync a registry cache in a
folder non-administrators can modify (naming the setting to change), an ordinary
run is unaffected, and an elevated ``manager init`` places the cache beside the
configuration instead of in the user profile.

Real: the command line and (on Windows) access-control checks of a temporary
folder. Mocked: the process's elevation state. Out of scope: downloading or
validating registry content.
"""

from __future__ import annotations

import io
import os
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from gupkg import cli, elevation, manager
from gupkg.registry import registry_status


@pytest.mark.skipif(os.name != "nt", reason="reads Windows access-control lists")
def test_elevated_run_refuses_a_user_writable_registry_cache(tmp_path: Path) -> None:
    """Under elevation, status of a cache in a user-writable folder fails with a pointer to the setting."""
    with mock.patch.object(elevation, "is_elevated", return_value=True):
        with pytest.raises(Exception, match=r"\[registry\] cache"):
            registry_status(tmp_path / "cache")


def test_ordinary_run_reads_any_registry_cache_location(tmp_path: Path) -> None:
    """Without elevation the cache location is unrestricted and an empty cache is simply empty."""
    with mock.patch.object(elevation, "is_elevated", return_value=False):
        assert registry_status(tmp_path / "cache").revision is None


@pytest.mark.skipif(os.name != "nt", reason="reads Windows access-control lists")
def test_registry_status_command_reports_the_refusal_as_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The command line turns the refusal into a normal error result, not a crash."""
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.setattr("gupkg.manager.__file__", str(package_dir / "manager.py"))
    config = package_dir / "gupkg-config.toml"
    config.write_text(
        'mode = "manager"\nschema_version = 2\n[packages]\n'
        f"system = '{tmp_path / 'system'}'\nuser = '{tmp_path / 'user'}'\n"
        f"[bin]\nsystem = '{tmp_path / 'bs'}'\nuser = '{tmp_path / 'bu'}'\n"
        f"[registry]\ncache = '{tmp_path / 'cache'}'\n",
        encoding="utf-8",
    )
    output = io.StringIO()

    with (
        mock.patch.object(elevation, "is_elevated", return_value=True),
        mock.patch("gupkg.commands.require_trusted_directory"),
        redirect_stdout(output),
    ):
        code = cli.main(["--format", "toml", "manager", "registry", "status"])

    document = tomllib.loads(output.getvalue())
    assert code == 2 and document["ok"] is False
    assert "Refusing the registry cache" in document["errors"][0]


def test_default_registry_cache_lives_in_the_gupkg_version_app_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default cache is App\\registry beside the running gupkg version, elevated or not."""
    for name, folder in (("USERPROFILE", "profile"), ("LOCALAPPDATA", "local"), ("SYSTEMDRIVE", "drive")):
        monkeypatch.setenv(name, str(tmp_path / folder))
    version = tmp_path / "opt" / "gupkg" / "1.0.0"
    (version / "gupkg").mkdir(parents=True)
    monkeypatch.setattr("gupkg.manager.__file__", str(version / "gupkg" / "manager.py"))
    target = version / "gupkg" / "gupkg-config.toml"

    with mock.patch.object(elevation, "is_elevated", return_value=True):
        elevated = manager.default_manager_config(target)
    with mock.patch.object(elevation, "is_elevated", return_value=False):
        ordinary = manager.default_manager_config(target)

    assert elevated.registry_cache == ordinary.registry_cache == version / "App" / "registry"


def _init_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, program_data: Path) -> Path:
    """Point the default locations at *tmp_path* and return where the config will be written."""
    for name, folder in (("USERPROFILE", "profile"), ("LOCALAPPDATA", "local"), ("SYSTEMDRIVE", "drive")):
        monkeypatch.setenv(name, str(tmp_path / folder))
    monkeypatch.setenv("ProgramData", str(program_data))
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.setattr("gupkg.manager.__file__", str(package_dir / "manager.py"))
    return package_dir / "gupkg-config.toml"


def test_init_accepts_a_cache_inside_the_system_root_when_it_is_in_a_package_app_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gupkg version's own App\\registry may sit under the system package root."""
    config_path = _init_environment(monkeypatch, tmp_path, tmp_path / "unused")
    version = tmp_path / "drive" / "opt" / "gupkg" / "1.0.0"
    (version / "gupkg").mkdir(parents=True)
    monkeypatch.setattr("gupkg.manager.__file__", str(version / "gupkg" / "manager.py"))
    output = io.StringIO()

    with mock.patch.object(elevation, "is_elevated", return_value=False), redirect_stdout(output):
        code = cli.main(["--format", "toml", "manager", "--config", str(config_path), "init"])

    assert code == 0, output.getvalue()
    assert manager.load_manager_config(config_path).registry_cache == version / "App" / "registry"


def test_failed_init_leaves_no_configuration_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache inside a package root (outside any App folder) is reported and no config file is written."""
    config_path = _init_environment(monkeypatch, tmp_path, tmp_path / "unused")
    inside_root = tmp_path / "drive" / "opt" / "cache"
    monkeypatch.setattr("gupkg.manager.default_registry_cache", lambda: inside_root)
    output = io.StringIO()

    with mock.patch.object(elevation, "is_elevated", return_value=True), redirect_stdout(output):
        code = cli.main(["--format", "toml", "manager", "init"])

    assert code == 2
    assert "outside both package roots" in tomllib.loads(output.getvalue())["errors"][0]
    assert not config_path.exists()
    assert list(config_path.parent.glob("*.check")) == []
