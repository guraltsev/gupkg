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


def test_elevated_initialization_keeps_the_cache_outside_the_user_profile_and_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An elevated `manager init` defaults the cache to ProgramData, which also keeps it outside the package roots."""
    for name, folder in (("USERPROFILE", "profile"), ("LOCALAPPDATA", "local"), ("SYSTEMDRIVE", "drive"), ("ProgramData", "programdata")):
        monkeypatch.setenv(name, str(tmp_path / folder))
    target = tmp_path / "package" / "gupkg-config.toml"

    with mock.patch.object(elevation, "is_elevated", return_value=True):
        elevated = manager.default_manager_config(target)
    with mock.patch.object(elevation, "is_elevated", return_value=False):
        ordinary = manager.default_manager_config(target)

    assert elevated.registry_cache == tmp_path / "programdata" / "gupkg" / "registry"
    assert ordinary.registry_cache == tmp_path / "local" / "gupkg" / "registry"


def _init_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, program_data: Path) -> Path:
    """Point the default locations at *tmp_path* and return where the config will be written."""
    for name, folder in (("USERPROFILE", "profile"), ("LOCALAPPDATA", "local"), ("SYSTEMDRIVE", "drive")):
        monkeypatch.setenv(name, str(tmp_path / folder))
    monkeypatch.setenv("ProgramData", str(program_data))
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.setattr("gupkg.manager.__file__", str(package_dir / "manager.py"))
    return package_dir / "gupkg-config.toml"


def test_elevated_init_creates_and_restricts_a_new_registry_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An elevated `manager init` creates the cache and restricts it to administrators once."""
    config_path = _init_environment(monkeypatch, tmp_path, tmp_path / "programdata")
    restricted: list[list[str]] = []

    def fake_run(command, **kwargs):
        restricted.append([str(part) for part in command])
        return mock.Mock(returncode=0, stdout="", stderr="")

    with (
        mock.patch.object(elevation, "is_elevated", return_value=True),
        mock.patch("subprocess.run", fake_run),
        redirect_stdout(io.StringIO()),
    ):
        code = cli.main(["manager", "init"])

    cache = tmp_path / "programdata" / "gupkg" / "registry"
    assert code == 0 and config_path.is_file() and cache.is_dir()
    assert restricted and all(str(cache) in call for call in restricted)
    assert any("*S-1-5-32-544:(OI)(CI)F" in part for call in restricted for part in call)


def test_failed_init_leaves_no_configuration_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid default (cache inside a package root) is reported and no config file is written."""
    config_path = _init_environment(monkeypatch, tmp_path, tmp_path / "drive" / "opt" / "inside-the-root")
    output = io.StringIO()

    with mock.patch.object(elevation, "is_elevated", return_value=True), redirect_stdout(output):
        code = cli.main(["--format", "toml", "manager", "init"])

    assert code == 2
    assert "outside both package roots" in tomllib.loads(output.getvalue())["errors"][0]
    assert not config_path.exists()
    assert list(config_path.parent.glob("*.check")) == []
