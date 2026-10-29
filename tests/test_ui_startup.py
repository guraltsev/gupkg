"""Cover how a UI program starts: it manages the one scope of its own process and refuses only a gupkg tree that standard users can change.

Behaviour covered: ``build_environment`` on an elevated machine with the gupkg installed in an admin-only root succeeds and its scope is the system's, on that root;
on a normal machine the scope is the user's; an elevated run from a tree that standard users can change is refused with ``StartupError`` (exit code 2) and the
message of the scope; ``scope_label`` is ``system · <root>`` or ``user · <root>``; ``checksum_policy`` is read from the run's own layers.

Real: ``gupkg_ui.startup`` and ``gupkg.scope.build_scope`` on files below pytest's temporary folder. Fake: ``FakeSystem`` answers the elevation and admin-only questions.
Out of scope: the screens that show the label (``test_ui_screens.py``) and the command lines (``test_manager_cli.py``).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from gupkg.scope import NOT_ADMIN_ONLY_REFUSAL
from gupkg.system.port import Mode
from gupkg_ui.startup import StartupError, build_environment
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401 - autouse fixture
from tests.test_scope import Machine


def environment(machine: Machine):
    """Build the UI environment of a run on *machine* with a stand-in ``gupkg`` command."""
    return build_environment(argparse.Namespace(), location=machine.location, system=machine.fake, env=machine.env, command=("gupkg",))


def test_an_elevated_run_with_an_admin_only_gupkg_starts_and_its_scope_is_the_systems(tmp_path: Path) -> None:
    """No refusal for the elevated shell: the program manages the system root, and says so."""
    machine = Machine(tmp_path, elevated=True)
    machine.app_settings()

    env = environment(machine)

    assert env.scope.mode is Mode.SYSTEM and env.scope.root == machine.system_root
    assert env.scope_label == f"system · {machine.system_root}" and env.settings.root == machine.system_root


def test_a_normal_run_manages_the_users_root(tmp_path: Path) -> None:
    """A normal token gives the user scope, and the label names it."""
    machine = Machine(tmp_path)
    machine.app_settings()

    env = environment(machine)

    assert env.scope.mode is Mode.USER and env.scope_label == f"user · {machine.user_root}"


def test_an_elevated_run_from_a_tree_standard_users_can_change_is_refused_with_the_scopes_message(tmp_path: Path) -> None:
    """The only refusal left: the administrator never runs a gupkg a standard user can change."""
    machine = Machine(tmp_path, elevated=True, untrusted={tmp_path / "sys" / "opt"})
    machine.app_settings()

    with pytest.raises(StartupError) as refused:
        environment(machine)

    assert refused.value.code == 2 and NOT_ADMIN_ONLY_REFUSAL in str(refused.value)


@pytest.mark.parametrize("value", ["ask", "refuse", "skip"])
def test_the_checksum_policy_is_read_from_the_layers_of_the_run(tmp_path: Path, value: str) -> None:
    """``[manager] checksum_missing`` of the app layer (which an elevated run reads too) is the policy, with no per-target rule."""
    machine = Machine(tmp_path, elevated=True)
    machine.app_settings(f'[manager]\nchecksum_missing = "{value}"\n')

    assert environment(machine).settings.checksum_policy == value
