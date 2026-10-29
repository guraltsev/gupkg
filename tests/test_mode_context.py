"""Cover the immutable ``ModeContext`` of design 6.1: roots, bin and Start Menu folders, environment target, layers, logs.

Behavior covered: for ``system`` and ``user`` the context takes root, bin, Start Menu and
environment target from ``[roots]`` and ``[integration]`` (the built-in values and non-default ones from a
settings layer); a bin or Start Menu folder that no layer set follows the machine's own default folder;
a user root with ``[integration] integrate = "none"`` keeps shims in ``<root>\\bin``, touches neither the Start Menu
nor the environment and offers the ``PATH`` line; the log folders follow D26.

Real: ``build_mode_context`` and ``load_settings`` on settings files below pytest's temporary folder. Fake: ``FakeSystem`` for the machine's default folders. Out of scope: selecting the mode
(``test_mode_selection.py``) and resolving a target (``test_mode_resolve.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.mode import ModeChoice, ModeContext, build_mode_context
from gupkg.model.settings import default_layer, merge_layers
from gupkg.modes import default_folders
from gupkg.system.port import Scope
from tests.mode_support import Machine, pin_no_ambient_elevation  # noqa: F401

ENV = {"USERPROFILE": "C:\\Users\\pat", "APPDATA": "C:\\Users\\pat\\AppData\\Roaming", "ProgramData": "C:\\ProgramData"}


def write_root_settings(root: Path, tables: str) -> None:
    """Write ``<root>\\.gupkg\\settings.toml`` with *tables* after ``schema = 4``."""
    path = root / ".gupkg" / "settings.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("schema = 4\n" + tables, encoding="utf-8")


def builtin(mode: str, **kwargs) -> ModeContext:
    """Build the context of *mode* from the built-in defaults alone."""
    return build_mode_context(ModeChoice(mode, "test"), merge_layers([default_layer(mode)]), ENV, **kwargs)


@pytest.mark.parametrize(
    ("mode", "root", "bin_dir", "start_menu", "env_target", "elevation"),
    [
        pytest.param(
            "system", "C:\\opt", "C:\\bin", "C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\opt", "HKLM", True,
            id="system-row-of-the-table",
        ),
        pytest.param(
            "user", "C:\\Users\\pat\\opt", "C:\\Users\\pat\\bin",
            "C:\\Users\\pat\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\opt", "HKCU", False, id="user-row-of-the-table",
        ),
    ],
)
def test_the_built_in_settings_give_the_rows_of_the_design_table(
    mode: str, root: str, bin_dir: str, start_menu: str, env_target: str, elevation: bool
) -> None:
    """Each mode's default root, bin, Start Menu, environment target and elevation need match design 6.1."""
    context = builtin(mode)

    assert context.mode == mode
    assert (context.root, context.bin_dir, context.start_menu) == (Path(root), Path(bin_dir), Path(start_menu))
    assert (context.env_target, context.requires_elevation) == (env_target, elevation)


def test_configured_values_replace_the_defaults_for_system_and_user(tmp_path: Path) -> None:
    """Roots, bins and Start Menus set in settings layers (including non-default ones) reach the context."""
    machine = Machine(tmp_path)
    write_root_settings(machine.system_root, f"[integration]\nbin = '{tmp_path / 'sysbin'}'\nstart_menu = '{tmp_path / 'sysmenu'}'\n")
    write_root_settings(machine.user_root, f"[integration]\nbin = '{tmp_path / 'userbin'}'\nstart_menu = '%APPDATA%\\Custom\\opt'\n")

    system = build_mode_context(ModeChoice("system", "t"), machine.loader()(system_mode=True).settings, machine.env)
    user = build_mode_context(ModeChoice("user", "t"), machine.loader()().settings, machine.env)

    assert (system.root, system.bin_dir, system.start_menu) == (machine.system_root, tmp_path / "sysbin", tmp_path / "sysmenu")
    assert (user.root, user.bin_dir) == (machine.user_root, tmp_path / "userbin")
    assert user.start_menu == Path(machine.env["APPDATA"]) / "Custom" / "opt"


def test_a_folder_no_layer_set_follows_the_machines_default_and_a_configured_one_wins(tmp_path: Path) -> None:
    """The machine's own default folder replaces only an untouched built-in value (a sandbox stays off the real machine)."""
    machine = Machine(tmp_path)
    write_root_settings(machine.user_root, f"[integration]\nbin = '{tmp_path / 'chosen'}'\n")
    defaults = default_folders(machine.fake)

    user = build_mode_context(ModeChoice("user", "t"), machine.loader()().settings, machine.env, defaults=defaults)
    system = build_mode_context(ModeChoice("system", "t"), machine.loader()(system_mode=True).settings, machine.env, defaults=defaults)

    assert user.bin_dir == tmp_path / "chosen"
    assert user.start_menu == machine.fake.scope_folders(Scope.USER).shortcut_root
    assert system.bin_dir == machine.fake.scope_folders(Scope.MACHINE).bin_dir


def test_user_integrate_none_touches_no_start_menu_or_environment_and_offers_the_path_line(tmp_path: Path) -> None:
    """A user root with ``integrate = "none"`` keeps shims in ``<root>\\bin`` and prints the ``PATH`` line to use."""
    machine = Machine(tmp_path)
    machine.write_app_settings(tables="[integration]\nintegrate = 'none'\n")
    settings = machine.loader()().settings

    context = build_mode_context(ModeChoice("user", "t"), settings, machine.env)

    assert (context.root, context.bin_dir, context.start_menu, context.env_target) == (
        machine.user_root, machine.user_root / "bin", None, None
    )
    assert context.integrate == "none" and not context.requires_elevation
    assert context.path_line() == f'set "PATH={machine.user_root / "bin"};%PATH%"'


def test_user_integrate_full_uses_the_user_profile_and_has_no_path_line(tmp_path: Path) -> None:
    """The default user integration goes to the user's Start Menu and ``HKCU``; nothing needs a ``PATH`` line."""
    machine = Machine(tmp_path)
    settings = machine.loader()().settings

    context = build_mode_context(ModeChoice("user", "t"), settings, machine.env)

    assert (context.env_target, context.integrate) == ("HKCU", "full")
    assert context.start_menu is not None
    assert context.path_line() is None


@pytest.mark.parametrize(
    ("mode", "layers"),
    [
        pytest.param("system", ("defaults", "app", "root"), id="system-takes-no-settings-option"),
        pytest.param("user", ("defaults", "app", "root", "cli"), id="user-adds-the-settings-option"),
    ],
)
def test_the_context_names_the_settings_layers_of_its_mode(mode: str, layers: tuple[str, ...]) -> None:
    """Design 6.2: defaults, ``$App``, the run's own root; plus ``--settings`` in user mode only."""
    assert builtin(mode).layers == layers


def test_log_directories_follow_decision_d26() -> None:
    """Per-package logs live in ``<root>\\<name>\\.gupkg\\logs`` and the root's own folder holds global run information."""
    context = builtin("user")

    assert context.log_dir == context.root / ".gupkg" / "logs"
    assert context.package_log_dir("tool") == context.root / "tool" / ".gupkg" / "logs"
    assert context.package_log_dir("group/tool") == context.root / "group" / "tool" / ".gupkg" / "logs"
    assert context.package_data_dir("tool") == context.root / "tool" / "data"


def test_the_context_is_immutable() -> None:
    """A resolved context cannot be changed by a subsystem (it is resolved once per operation)."""
    with pytest.raises(AttributeError):
        builtin("user").mode = "system"  # type: ignore[misc]


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
    context = builtin("user")

    with pytest.raises(ValueError, match="not a safe package name"):
        context.package_dir(name)
    with pytest.raises(ValueError, match="not a safe package name"):
        context.package_log_dir(name)
