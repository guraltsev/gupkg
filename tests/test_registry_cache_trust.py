"""Cover which registry cache locations an elevated run accepts.

Behavior covered: an elevated run refuses to read or sync a registry cache in a
folder non-administrators can modify (naming the setting to change), an ordinary
run is unaffected, and an elevated ``manager init`` places the cache beside the
configuration instead of in the user profile.

Real: the command line and the registry cache logic. Stood in by ``FakeSystem``:
the process's elevation state and which folders are admin-only. Out of scope:
the Windows access-control reader itself (see ``tests/test_system_contract.py``)
and downloading or validating registry content.
"""

from __future__ import annotations

import io
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from gupkg import cli, manager
from gupkg.registry import registry_status
from gupkg.system.fake import FakeSystem


def test_elevated_run_refuses_a_user_writable_registry_cache(tmp_path: Path) -> None:
    """Under elevation, status of a cache in a user-writable folder fails with a pointer to the setting."""
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={tmp_path})

    with pytest.raises(Exception, match=r"\[registry\] cache"):
        registry_status(tmp_path / "cache", system)


def test_elevated_run_accepts_a_registry_cache_in_an_admin_only_folder(tmp_path: Path) -> None:
    """Under elevation, a cache whose folder only administrators can modify is read normally."""
    system = FakeSystem(tmp_path / "machine", elevated=True)

    assert registry_status(tmp_path / "cache", system).revision is None


def test_ordinary_run_reads_any_registry_cache_location(tmp_path: Path) -> None:
    """Without elevation the cache location is unrestricted and an empty cache is simply empty."""
    system = FakeSystem(tmp_path / "machine", elevated=False, untrusted_dirs={tmp_path})

    assert registry_status(tmp_path / "cache", system).revision is None


def test_registry_status_command_reports_the_refusal_as_an_error(tmp_path: Path) -> None:
    """The command line turns the refusal into a normal error result, not a crash."""
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (tmp_path / "cache").mkdir()
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={tmp_path / "cache"})
    config = package_dir / "gupkg-config.toml"
    config.write_text(
        'mode = "manager"\nschema_version = 2\n[packages]\n'
        f"system = '{tmp_path / 'system'}'\nuser = '{tmp_path / 'user'}'\n"
        f"[bin]\nsystem = '{tmp_path / 'bs'}'\nuser = '{tmp_path / 'bu'}'\n"
        f"[registry]\ncache = '{tmp_path / 'cache'}'\n",
        encoding="utf-8",
    )
    output = io.StringIO()

    with redirect_stdout(output):
        code = cli.main(
            ["--format", "toml", "manager", "--config", str(config), "registry", "status"], system=system
        )

    document = tomllib.loads(output.getvalue())
    assert code == 2 and document["ok"] is False
    assert "Refusing the registry cache" in document["errors"][0]


def test_default_registry_cache_lives_in_the_gupkg_version_app_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default cache is App\\registry beside the running gupkg version."""
    for name, folder in (("USERPROFILE", "profile"), ("LOCALAPPDATA", "local"), ("SYSTEMDRIVE", "drive")):
        monkeypatch.setenv(name, str(tmp_path / folder))
    version = tmp_path / "opt" / "gupkg" / "1.0.0"
    (version / "gupkg").mkdir(parents=True)
    monkeypatch.setattr("gupkg.manager.__file__", str(version / "gupkg" / "manager.py"))
    target = version / "gupkg" / "gupkg-config.toml"

    config = manager.default_manager_config(target)

    assert config.registry_cache == version / "App" / "registry"


def _init_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, program_data: Path) -> Path:
    """Point the default locations at *tmp_path* and return where the config will be written."""
    for name, folder in (("USERPROFILE", "profile"), ("LOCALAPPDATA", "local"), ("SYSTEMDRIVE", "drive")):
        monkeypatch.setenv(name, str(tmp_path / folder))
    monkeypatch.setenv("ProgramData", str(program_data))
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.setattr("gupkg.manager.__file__", str(package_dir / "manager.py"))
    return package_dir / "gupkg-config.toml"


def test_init_accepts_a_cache_inside_the_system_root_when_it_is_in_a_package_app_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: FakeSystem
) -> None:
    """The gupkg version's own App\\registry may sit under the system package root."""
    config_path = _init_environment(monkeypatch, tmp_path, tmp_path / "unused")
    version = tmp_path / "drive" / "opt" / "gupkg" / "1.0.0"
    (version / "gupkg").mkdir(parents=True)
    monkeypatch.setattr("gupkg.manager.__file__", str(version / "gupkg" / "manager.py"))
    output = io.StringIO()

    with redirect_stdout(output):
        code = cli.main(["--format", "toml", "manager", "--config", str(config_path), "init"], system=system)

    assert code == 0, output.getvalue()
    assert manager.load_manager_config(config_path).registry_cache == version / "App" / "registry"


def test_failed_init_leaves_no_configuration_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: FakeSystem
) -> None:
    """A cache inside a package root (outside any App folder) is reported and no config file is written."""
    config_path = _init_environment(monkeypatch, tmp_path, tmp_path / "unused")
    inside_root = tmp_path / "drive" / "opt" / "cache"
    monkeypatch.setattr("gupkg.manager.default_registry_cache", lambda: inside_root)
    output = io.StringIO()

    system.elevated = True
    with redirect_stdout(output):
        code = cli.main(["--format", "toml", "manager", "init"], system=system)

    assert code == 2
    assert "outside both package roots" in tomllib.loads(output.getvalue())["errors"][0]
    assert not config_path.exists()
    assert list(config_path.parent.glob("*.check")) == []
