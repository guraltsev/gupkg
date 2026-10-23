"""Cover manager configuration validation and scoped local inventory.

These tests exercise real TOML files and temporary package layouts. Network
providers, Textual, and filesystem permission simulation are out of scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from gupkg.core import ConfigValidationError, Scope
from gupkg.manager import discover_manager, load_manager_config


def _write_config(path: Path, system: str, user: str, *, extra: str = "", schema_version: int = 2) -> None:
    """Create a manager configuration with the requested two roots."""
    path.write_text(
        f'mode = "manager"\nschema_version = {schema_version}\n{extra}\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n\n"
        "[bin]\n"
        f"system = '{path.parent / 'system-bin'}'\nuser = '{path.parent / 'user-bin'}'\n\n"
        "[registry]\n"
        f"cache = '{path.parent / 'registry-cache'}'\n\n"
        "[shims]\nlinkage = 'dynamic'\n",
        encoding="utf-8",
    )


def _manifest(root: Path, selector: str, version: str) -> None:
    """Create a minimal manifest-backed package version."""
    manifest = root / selector / version / "pkg.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f'name = "{selector.rsplit("/", 1)[-1]}"\nversion = "{version[1:].rsplit(".l", 1)[0]}"\nlocalVersion = {version.rsplit(".l", 1)[1]}\n',
        encoding="utf-8",
    )


def test_manager_config_expands_relative_and_case_insensitive_environment_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Manager roots resolve relative to the file and environment names case-insensitively."""
    config_dir = tmp_path / "manager"
    config_dir.mkdir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    config_path = config_dir / "gupkg-config.toml"
    _write_config(config_path, "system", "%userprofile%\\Programs")

    config = load_manager_config(config_path)

    assert config.system_root == (config_dir / "system").resolve()
    assert config.user_root == (tmp_path / "profile" / "Programs").resolve()


def test_manager_config_rejects_unknown_variables_and_nested_roots(tmp_path: Path) -> None:
    """Invalid expansion and overlapping roots fail before inventory discovery."""
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "root", "root/user")
    with pytest.raises(ConfigValidationError, match="non-nested|nested"):
        load_manager_config(config_path)

    _write_config(config_path, "%MISSING_MANAGER_ROOT%", "user")
    with pytest.raises(ConfigValidationError, match="MISSING_MANAGER_ROOT"):
        load_manager_config(config_path)


def test_manager_config_rejects_wrong_mode_schema_and_keys(tmp_path: Path) -> None:
    """Manager configuration accepts only the version-two public schema."""
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")
    config_path.write_text(config_path.read_text().replace('mode = "manager"', 'mode = "other"'))
    with pytest.raises(ConfigValidationError, match="mode"):
        load_manager_config(config_path)
    _write_config(config_path, "system", "user", schema_version=1)
    with pytest.raises(ConfigValidationError, match="schema_version"):
        load_manager_config(config_path)
    _write_config(config_path, "system", "user", extra="unexpected = true")
    with pytest.raises(ConfigValidationError, match="unexpected"):
        load_manager_config(config_path)


def test_manager_inventory_keeps_duplicate_selectors_scoped_and_orders_versions(tmp_path: Path, system) -> None:
    """Duplicate selectors remain distinct and local versions use semantic ordering."""
    system_root = tmp_path / "system"
    user = tmp_path / "user"
    system_root.mkdir()
    user.mkdir()
    _manifest(user, "vscode", "1.9.0.local.1")
    _manifest(user, "vscode", "1.10.0.local.1")
    _manifest(system_root, "vscode", "2.0.0.local.1")
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")

    inventory = discover_manager(load_manager_config(config_path), system)

    assert [target.target_id for target in inventory.targets] == ["user:vscode", "system:vscode"]
    assert inventory.targets[0].local_version == "1.10.0.local.1"
    assert all(target.installation_status == "not-installed" for target in inventory.targets)


def test_manager_inventory_marks_missing_root_incomplete_without_creating_it(tmp_path: Path, system) -> None:
    """A missing configured root is reported incomplete and remains absent."""
    system_root = tmp_path / "system"
    system_root.mkdir()
    missing_user = tmp_path / "missing-user"
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "missing-user")

    inventory = discover_manager(load_manager_config(config_path), system)

    user_scope = next(scope for scope in inventory.scopes if scope.scope == Scope.USER)
    assert not user_scope.complete
    assert not missing_user.exists()


def test_manager_inventory_distinguishes_missing_and_broken_current(tmp_path: Path, system) -> None:
    """A sole local version is not installed and a regular current entry is broken."""
    system_root = tmp_path / "system"
    user = tmp_path / "user"
    system_root.mkdir()
    user.mkdir()
    _manifest(user, "not-installed", "1.0.0.local.1")
    _manifest(system_root, "broken", "1.0.0.local.1")
    (system_root / "broken" / "current").mkdir()
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")

    targets = discover_manager(load_manager_config(config_path), system).targets

    assert targets[0].installation_status == "broken"
    assert targets[1].installation_status == "not-installed"


def _saved_update_state(root: Path, selector: str, body: str) -> Path:
    """Write the package-root update state file that update checks leave behind."""
    path = root / selector / ".gupkg" / "state" / "update.toml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_discovery_restores_a_saved_available_update(tmp_path: Path, system) -> None:
    """A saved check with a newer candidate is listed as available with its check time."""
    system_root, user = tmp_path / "system", tmp_path / "user"
    system_root.mkdir()
    user.mkdir()
    _manifest(user, "tool", "1.0.0.local.1")
    _saved_update_state(
        user, "tool",
        'schemaVersion = 1\nlastSuccessfulCheck = "2026-10-17T14:02:00+00:00"\n'
        'lastStatus = "available"\nlastCandidateVersion = "1.1.0"\n',
    )
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")

    [target] = discover_manager(load_manager_config(config_path), system).targets

    assert target.update_status == "available"
    assert target.candidate_version == "1.1.0"
    assert target.checked_at == "2026-10-17T14:02:00+00:00"


def test_discovery_shows_current_once_the_saved_candidate_is_installed(tmp_path: Path, system) -> None:
    """A saved available update that is no newer than the active version shows as current."""
    system_root, user = tmp_path / "system", tmp_path / "user"
    system_root.mkdir()
    user.mkdir()
    _manifest(user, "tool", "1.0.0.local.1")
    _saved_update_state(
        user, "tool",
        'lastSuccessfulCheck = "2026-10-17T14:02:00+00:00"\n'
        'lastStatus = "available"\nlastCandidateVersion = "1.0.0"\n',
    )
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")

    [target] = discover_manager(load_manager_config(config_path), system).targets

    assert target.update_status == "current"
    assert target.candidate_version is None


def test_discovery_reports_a_failed_last_check_as_error(tmp_path: Path, system) -> None:
    """A saved state whose last attempt failed is listed with the error status."""
    system_root, user = tmp_path / "system", tmp_path / "user"
    system_root.mkdir()
    user.mkdir()
    _manifest(user, "tool", "1.0.0.local.1")
    _saved_update_state(user, "tool", 'lastStatus = "current"\nlastError = "offline"\n')
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")

    [target] = discover_manager(load_manager_config(config_path), system).targets

    assert target.update_status == "error"


def test_discovery_leaves_a_corrupt_update_state_in_place_and_reports_unchecked(tmp_path: Path, system) -> None:
    """A corrupt saved state is treated as unchecked and is not renamed by discovery."""
    system_root, user = tmp_path / "system", tmp_path / "user"
    system_root.mkdir()
    user.mkdir()
    _manifest(user, "tool", "1.0.0.local.1")
    state = _saved_update_state(user, "tool", "this is [not toml")
    config_path = tmp_path / "gupkg-config.toml"
    _write_config(config_path, "system", "user")

    [target] = discover_manager(load_manager_config(config_path), system).targets

    assert target.update_status == "unchecked"
    assert state.read_text(encoding="utf-8") == "this is [not toml"
    assert list(state.parent.iterdir()) == [state]
