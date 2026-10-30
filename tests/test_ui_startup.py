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
from gupkg_ui.startup import StartupError, build_environment, shorten_path
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401 - autouse fixture
from gupkg.settingsload import SOURCE_ROOT_OPTION
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


def test_root_names_the_root_of_the_run_and_is_relayed_to_every_call(tmp_path: Path) -> None:
    """``--root DIR`` is row 1 of the root chain: the scope is on that folder, says so, and the client passes the option to every ``gupkg`` call."""
    machine = Machine(tmp_path)
    machine.app_settings()
    chosen = tmp_path / "chosen"
    chosen.mkdir()

    env = build_environment(argparse.Namespace(root=chosen), location=machine.location, system=machine.fake, env=machine.env, command=("gupkg",))

    assert env.scope.root == chosen and env.scope.root_source == SOURCE_ROOT_OPTION and env.scope.mode is Mode.USER
    assert env.client.root_options == ("--root", str(chosen))


def test_an_elevated_run_with_a_root_that_standard_users_can_change_stops_at_startup_and_creates_nothing(tmp_path: Path) -> None:
    """System mode needs an admin-only root: the scope refuses it (StartupError, exit code 2) and the folder is left as it was."""
    chosen = tmp_path / "user-writable"
    chosen.mkdir()
    machine = Machine(tmp_path, elevated=True, untrusted={chosen})
    machine.app_settings()

    with pytest.raises(StartupError) as refused:
        build_environment(argparse.Namespace(root=chosen), location=machine.location, system=machine.fake, env=machine.env, command=("gupkg",))

    assert refused.value.code == 2 and "is not admin-only" in str(refused.value) and list(chosen.iterdir()) == []


@pytest.mark.parametrize("module", ["gupkg_manager.cli", "gupkg_tui.cli"])
def test_each_program_lists_root_in_its_help(module: str, capsys: pytest.CaptureFixture[str]) -> None:
    """``gupkg-manager --help`` and ``gupkg-tui --help`` document ``--root DIR``."""
    import importlib

    assert importlib.import_module(module).main(["--help"]) == 0
    assert "--root DIR" in capsys.readouterr().out


@pytest.mark.parametrize("module", ["gupkg_manager.cli", "gupkg_tui.cli"])
def test_each_program_stops_at_startup_with_the_scopes_message_for_an_unsuitable_root(tmp_path: Path, capsys: pytest.CaptureFixture[str], module: str) -> None:
    """An elevated program started with ``--root`` on a user-writable folder prints the scope's message and returns 2; the folder stays empty."""
    import importlib

    chosen = tmp_path / "user-writable"
    chosen.mkdir()
    machine = Machine(tmp_path, elevated=True, untrusted={chosen})
    machine.app_settings()
    argv = ["--root", str(chosen)] + (["alpha"] if module == "gupkg_tui.cli" else [])

    code = importlib.import_module(module).main(argv, location=machine.location, system=machine.fake, env=machine.env, command=("gupkg",))

    assert code == 2 and "is not admin-only" in capsys.readouterr().err and list(chosen.iterdir()) == []


@pytest.mark.parametrize("value", ["ask", "refuse", "skip"])
def test_the_checksum_policy_is_read_from_the_layers_of_the_run(tmp_path: Path, value: str) -> None:
    """``[manager] checksum_missing`` of the app layer (which an elevated run reads too) is the policy, with no per-target rule."""
    machine = Machine(tmp_path, elevated=True)
    machine.app_settings(f'[manager]\nchecksum_missing = "{value}"\n')

    assert environment(machine).settings.checksum_policy == value


@pytest.mark.parametrize(
    ("path", "home", "expected"),
    [
        pytest.param("C:\\opt", "C:\\Users\\me", "C:\\opt", id="short-stays"),
        pytest.param("C:\\Users\\me\\opt", "C:\\Users\\me", "~\\opt", id="home-is-tilde"),
        pytest.param("C:\\Users\\mess\\opt", "C:\\Users\\me", "C:\\Users\\mess\\opt", id="a-longer-name-is-not-home"),
        pytest.param("\\\\fileserver\\share\\team\\projects\\gupkg\\root\\opt\\very\\long\\path", None, "\\\\fileserver\\share\\…\\opt\\very\\long\\path", id="unc-keeps-server-and-share"),
        pytest.param("D:\\a\\very\\long\\folder\\chain\\that\\goes\\on\\and\\on\\and\\on\\roots\\user", None, "D:\\…\\goes\\on\\and\\on\\and\\on\\roots\\user", id="middle-is-cut"),
    ],
)
def test_a_root_is_shortened_for_the_status_line(path: str, home: str | None, expected: str) -> None:
    """The home folder is ``~``, and a long path keeps the drive and the last folders with the middle replaced by an ellipsis, in at most 40 characters."""
    shortened = shorten_path(path, home)

    assert shortened == expected and len(shortened) <= 40


def test_a_single_very_long_folder_name_is_cut_from_its_front(tmp_path: Path) -> None:
    """Even one huge last folder fits in the bound, keeping the end of its name."""
    shortened = shorten_path("C:\\" + "x" * 80 + "\\end")

    assert len(shortened) <= 40 and shortened.startswith("C:\\…\\") and shortened.endswith("end")


@pytest.mark.parametrize("module", ["gupkg_manager.cli", "gupkg_tui.cli"])
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_a_given_root_that_is_missing_or_not_a_folder_stops_at_startup_and_nothing_is_created(tmp_path: Path, capsys: pytest.CaptureFixture[str], module: str, kind: str) -> None:
    """``--root`` must name an existing folder: one-line message, exit code 2, nothing created at that path."""
    import importlib

    chosen = tmp_path / "chosen"
    if kind == "file":
        chosen.write_text("x", encoding="utf-8")
    machine = Machine(tmp_path)
    machine.app_settings()
    argv = ["--root", str(chosen)] + (["alpha"] if module == "gupkg_tui.cli" else [])

    code = importlib.import_module(module).main(argv, location=machine.location, system=machine.fake, env=machine.env, command=("gupkg",))

    expected = f"--root {chosen} is not a folder" if kind == "file" else f"--root {chosen} does not exist (create the folder first)"
    assert code == 2 and capsys.readouterr().err.strip().endswith(expected)
    assert chosen.is_file() if kind == "file" else not chosen.exists()


@pytest.mark.parametrize("word", ["manager", "tui"])
def test_through_gupkg_a_missing_root_is_reported_by_the_program_and_nothing_is_created(tmp_path: Path, capsys: pytest.CaptureFixture[str], word: str) -> None:
    """``gupkg --root X manager|tui`` forwards the option; the program it starts refuses the missing folder (exit 2) and creates nothing."""
    import importlib

    from gupkg import cli
    from gupkg.selfmgmt.location import RunningGupkg
    from gupkg.system.fake import FakeSystem
    from gupkg.system.port import ProcessResult

    chosen = tmp_path / "chosen"
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / f"gupkg-{word}.cmd").write_text("@echo off\r\n", encoding="ascii")
    system = FakeSystem(tmp_path / "forward", processes={f"gupkg-{word}.cmd": lambda call: ProcessResult(0)})
    cli.main(["--root", str(chosen), word, *(["alpha"] if word == "tui" else [])], system=system, env={}, location=RunningGupkg("source", tree / "gupkg", (tree,)))
    forwarded = system.foreground_log[0][1:]
    machine = Machine(tmp_path)
    machine.app_settings()

    code = importlib.import_module(f"gupkg_{word}.cli").main(forwarded, location=machine.location, system=machine.fake, env=machine.env, command=("gupkg",))

    assert forwarded[:2] == ["--root", str(chosen)]
    assert code == 2 and "does not exist (create the folder first)" in capsys.readouterr().err and not chosen.exists()

def test_a_default_or_configured_root_that_does_not_exist_yet_still_starts(tmp_path: Path) -> None:
    """The folder check applies only to a GIVEN ``--root``: the default and ``[roots]`` roots may be missing on a first run."""
    machine = Machine(tmp_path)
    machine.app_settings()

    env = environment(machine)

    assert not machine.user_root.exists() and env.scope.root == machine.user_root