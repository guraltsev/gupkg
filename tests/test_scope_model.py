"""Cover the immutable ``Scope`` of design 6.1: roots, bin and Start Menu folders, environment target, logs.

Behavior covered: for ``system`` and ``user`` the scope takes root, bin, Start Menu and environment target from ``[roots]``
and ``[integration]`` (the built-in values and non-default ones from a settings layer); a bin or Start Menu folder that no
layer set follows the machine's own default folder; ``integrate = "none"`` keeps shims in ``<root>\\bin``, touches neither
the Start Menu nor the environment and offers the ``PATH`` line, in user mode and in system mode; the log folders follow D26;
a package name cannot leave the root; the scope is frozen.

Real: ``make_scope`` and ``build_scope`` on settings files below pytest's temporary folder. Fake: ``FakeSystem`` for the
machine's default folders. Out of scope: building the scope of a process (``test_scope.py``) and resolving a target
(``test_targets.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.scope import Scope, make_scope
from gupkg.model.settings import default_layer, expand_env, merge_layers
from gupkg.system.port import Mode
from tests.mode_support import Machine, pin_no_ambient_elevation  # noqa: F401

ENV = {"USERPROFILE": "C:\\Users\\pat", "APPDATA": "C:\\Users\\pat\\AppData\\Roaming", "ProgramData": "C:\\ProgramData"}


def write_root_settings(root: Path, tables: str) -> None:
    """Write ``<root>\\.gupkg\\settings.toml`` with *tables* after ``schema = 4``."""
    path = root / ".gupkg" / "settings.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("schema = 4\n" + tables, encoding="utf-8")


def builtin(mode: str, **kwargs) -> Scope:
    """Build the scope of *mode* from the built-in defaults alone."""
    settings = merge_layers([default_layer(mode)])
    return make_scope(Mode(mode), Path(expand_env(settings[f"roots.{mode}"], ENV)), "built-in default", settings, ENV, **kwargs)


@pytest.mark.parametrize(
    ("mode", "root", "bin_dir", "start_menu", "env_target"),
    [
        pytest.param(
            "system", "C:\\opt", "C:\\bin", "C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\opt", "HKLM",
            id="system-row-of-the-table",
        ),
        pytest.param(
            "user", "C:\\Users\\pat\\opt", "C:\\Users\\pat\\bin",
            "C:\\Users\\pat\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\opt", "HKCU", id="user-row-of-the-table",
        ),
    ],
)
def test_the_built_in_settings_give_the_rows_of_the_design_table(
    mode: str, root: str, bin_dir: str, start_menu: str, env_target: str
) -> None:
    """Each mode's default root, bin, Start Menu and environment target match design 6.1."""
    scope = builtin(mode)

    assert scope.mode is Mode(mode)
    assert (scope.root, scope.bin_dir, scope.start_menu) == (Path(root), Path(bin_dir), Path(start_menu))
    assert scope.env_target == env_target
    assert scope.root_source == "built-in default"


@pytest.mark.parametrize("mode", ["system", "user"])
def test_configured_values_replace_the_defaults(tmp_path: Path, mode: str) -> None:
    """Bins and Start Menus set in the root's settings layer reach the scope, in either mode."""
    machine = Machine(tmp_path, elevated=mode == "system")
    root = machine.system_root if mode == "system" else machine.user_root
    write_root_settings(root, f"[integration]\nbin = '{tmp_path / 'chosen-bin'}'\nstart_menu = '%APPDATA%\\Custom\\opt'\n")

    scope = machine.scope()

    assert (scope.root, scope.bin_dir) == (root, tmp_path / "chosen-bin")
    assert scope.start_menu == Path(machine.env["APPDATA"]) / "Custom" / "opt"


@pytest.mark.parametrize("mode", [Mode.SYSTEM, Mode.USER])
def test_a_folder_no_layer_set_follows_the_machines_default_and_a_configured_one_wins(tmp_path: Path, mode: Mode) -> None:
    """The machine's own default folder replaces only an untouched built-in value (a sandbox stays off the real machine)."""
    machine = Machine(tmp_path, elevated=mode is Mode.SYSTEM)
    folders = machine.fake.scope_folders(mode)

    plain = machine.scope()
    root = plain.root
    write_root_settings(root, f"[integration]\nbin = '{tmp_path / 'chosen'}'\n")
    chosen = machine.scope()

    assert (plain.bin_dir, plain.start_menu) == (folders.bin_dir, folders.shortcut_root)
    assert (chosen.bin_dir, chosen.start_menu) == (tmp_path / "chosen", folders.shortcut_root)


def test_user_integrate_none_touches_no_start_menu_or_environment_and_offers_the_path_line(tmp_path: Path) -> None:
    """A scope with ``integrate = "none"`` keeps shims in ``<root>\\bin`` and prints the ``PATH`` line to use."""
    machine = Machine(tmp_path)
    machine.write_app_settings(tables="[integration]\nintegrate = 'none'\n")

    scope = machine.scope()

    assert (scope.root, scope.bin_dir, scope.start_menu, scope.env_target) == (machine.user_root, machine.user_root / "bin", None, None)
    assert scope.integrate == "none"
    assert scope.path_line() == f'set "PATH={machine.user_root / "bin"};%PATH%"'


def test_system_integrate_none_is_honoured_too(tmp_path: Path) -> None:
    """``integrate`` is a property of a scope (issue 035 section 4.2): the system mode no longer forces ``full``."""
    machine = Machine(tmp_path, elevated=True)
    machine.write_app_settings(tables="[integration]\nintegrate = 'none'\n")

    scope = machine.scope()

    assert (scope.mode, scope.integrate, scope.start_menu, scope.env_target) == (Mode.SYSTEM, "none", None, None)
    assert scope.bin_dir == machine.system_root / "bin"


def test_user_integrate_full_uses_the_user_profile_and_has_no_path_line(tmp_path: Path) -> None:
    """The default user integration goes to the user's Start Menu and ``HKCU``; nothing needs a ``PATH`` line."""
    scope = Machine(tmp_path).scope()

    assert (scope.env_target, scope.integrate) == ("HKCU", "full")
    assert scope.start_menu is not None
    assert scope.path_line() is None


def test_log_directories_follow_decision_d26() -> None:
    """Per-package logs live in ``<root>\\<name>\\.gupkg\\logs`` and the root's own folder holds global run information."""
    scope = builtin("user")

    assert scope.log_dir == scope.root / ".gupkg" / "logs"
    assert scope.package_log_dir("tool") == scope.root / "tool" / ".gupkg" / "logs"
    assert scope.package_log_dir("group/tool") == scope.root / "group" / "tool" / ".gupkg" / "logs"
    assert scope.package_data_dir("tool") == scope.root / "tool" / "data"


def test_the_scope_is_immutable() -> None:
    """A scope cannot be changed by a subsystem (it is built once per process)."""
    with pytest.raises(AttributeError):
        builtin("user").mode = Mode.SYSTEM  # type: ignore[misc]


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("..\\evil", id="dotdot-with-separator"),
        pytest.param("../evil", id="dotdot-with-slash"),
        pytest.param("a/../../evil", id="dotdot-inside-groups"),
        pytest.param("C:evil", id="drive-colon"),
        pytest.param("", id="empty"),
        pytest.param("tool.", id="trailing-dot"),
    ],
)
def test_a_package_name_cannot_leave_the_root(name: str) -> None:
    """``package_dir`` (and so the log and data folders) refuses a name that would leave the root or is not a folder name."""
    scope = builtin("user")

    with pytest.raises(ValueError, match="not a safe package name"):
        scope.package_dir(name)
    with pytest.raises(ValueError, match="not a safe package name"):
        scope.package_log_dir(name)
