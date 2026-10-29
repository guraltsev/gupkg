"""Cover ``build_scope``: the mode comes from the token, the root from the fallback chain, and an elevated run trusts only an admin-only gupkg.

Behavior covered: a process that is not elevated gets a user scope and an elevated one a system scope, with no flag
anywhere (the ``GUPKG_ELEVATED`` marker of the launcher counts as elevated); the scope names its root, the source of the
root, the machine folders of its mode and the effective settings; ``integrate = "none"`` is honoured in both modes; an
elevated run whose gupkg tree can be changed by standard users is refused before anything is read from any root; a user
run never reads anything under the system root, even when the app layer names both roots; the Resolve text names mode,
root and source; ``check_supported`` compares a package's ``modes`` with the scope's mode and fills the command and the
package name into the system-only hint.

Real: ``gupkg.scope.build_scope`` and ``gupkg.settingsload`` on files below pytest's temporary folder. Fake:
``FakeSystem`` answers the elevation and the admin-only questions, and a recording ``read_text`` shows what is read.
Out of scope: the command line (``tests/test_cli_scope.py``) and how the engine uses the scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.scope import (
    PORTABLE_REFUSAL,
    SYSTEM_ONLY_REFUSAL,
    USER_ONLY_REFUSAL,
    ModeRefused,
    Scope,
    check_supported,
)
from gupkg.scope import NOT_ADMIN_ONLY_REFUSAL, build_scope
from gupkg.selfmgmt.location import RunningGupkg
from gupkg.settingsload import SOURCE_APP, SOURCE_INSTALLED_SYSTEM, SOURCE_INSTALLED_USER, SOURCE_ROOT_OPTION, read_text_file
from gupkg.system.fake import FakeSystem
from gupkg.system.port import Mode


class Machine:
    """A fake machine under a temporary folder: a system root that holds the gupkg tree, a user root and an app folder."""

    def __init__(self, base: Path, *, elevated: bool = False, untrusted: set[Path] | None = None) -> None:
        self.base = base
        self.system_root = base / "sys" / "opt"
        self.user_root = base / "home" / "opt"
        self.app = self.system_root / "gupkg" / "1.0.0" / "App"
        self.env = {"USERPROFILE": str(base / "home"), "APPDATA": str(base / "appdata"), "ProgramData": str(base / "programdata")}
        self.fake = FakeSystem(base / "fake", elevated=elevated, untrusted_dirs=untrusted or set())
        self.location = RunningGupkg(
            "installed", self.app / "gupkg", (self.app,), self.app.parent, self.system_root, self.app,
        )
        self.reads: list[Path] = []

    def app_settings(self, text: str = "") -> None:
        """Write ``$App\\settings.toml`` naming both roots (and *text* after them)."""
        self.app.mkdir(parents=True, exist_ok=True)
        (self.app / "settings.toml").write_text(
            f"schema = 4\n[roots]\nsystem = '{self.system_root}'\nuser = '{self.user_root}'\n" + text, encoding="utf-8"
        )

    def read_text(self, path: Path) -> str | None:
        """Read a settings file and remember that it was read."""
        self.reads.append(Path(path))
        return read_text_file(path)

    def scope(self, **options) -> Scope:
        """Build the scope of a run on this machine."""
        options.setdefault("location", self.location)
        return build_scope(system=self.fake, env=self.env, read_text=self.read_text, **options)


@pytest.fixture(autouse=True)
def no_ambient_elevation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the launcher's ``GUPKG_ELEVATED`` marker off, so elevation comes only from the fake machine."""
    monkeypatch.delenv("GUPKG_ELEVATED", raising=False)


def test_a_process_that_is_not_elevated_gets_a_user_scope(tmp_path: Path) -> None:
    """No flag: a normal token is user mode, on the root the app layer names, with the user's machine folders."""
    machine = Machine(tmp_path)
    machine.app_settings()

    scope = machine.scope()

    folders = machine.fake.scope_folders(Mode.USER)
    assert scope.mode is Mode.USER and scope.requires_elevation is False
    assert scope.root == machine.user_root and scope.root_source == SOURCE_APP
    assert scope.bin_dir == folders.bin_dir and scope.start_menu == folders.shortcut_root and scope.env_target == "HKCU"
    assert scope.integrate == "full" and scope.settings["roots.user"] == str(machine.user_root)


def test_an_elevated_process_gets_a_system_scope_on_the_root_of_the_installed_gupkg(tmp_path: Path) -> None:
    """No flag: an elevated token is system mode, on the admin-only root this gupkg is installed in."""
    machine = Machine(tmp_path, elevated=True)
    machine.app_settings()

    scope = machine.scope()

    folders = machine.fake.scope_folders(Mode.SYSTEM)
    assert scope.mode is Mode.SYSTEM and scope.requires_elevation is True
    assert scope.root == machine.system_root and scope.root_source == SOURCE_INSTALLED_SYSTEM
    assert scope.bin_dir == folders.bin_dir and scope.start_menu == folders.shortcut_root and scope.env_target == "HKLM"


def test_the_launchers_elevation_marker_counts_as_elevated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``GUPKG_ELEVATED=1`` can only make a run stricter: the scope is the system's even when the token says otherwise."""
    machine = Machine(tmp_path)
    machine.app_settings()
    monkeypatch.setenv("GUPKG_ELEVATED", "1")

    assert machine.scope().mode is Mode.SYSTEM


def test_a_user_run_in_a_user_writable_gupkg_root_uses_that_root(tmp_path: Path) -> None:
    """A user-mode gupkg installed in a user-writable root works on that root, and ``--root`` beats it."""
    machine = Machine(tmp_path, untrusted={tmp_path / "sys"})
    machine.app_settings()
    chosen = tmp_path / "elsewhere"

    assert (machine.scope().root, machine.scope().root_source) == (machine.system_root, SOURCE_INSTALLED_USER)
    option = machine.scope(root_option=chosen)
    assert (option.root, option.root_source) == (chosen, SOURCE_ROOT_OPTION)


@pytest.mark.parametrize("elevated", [False, True], ids=["user", "system"])
def test_integrate_none_is_a_property_of_the_scope_in_both_modes(tmp_path: Path, elevated: bool) -> None:
    """``[integration] integrate = "none"`` puts shims in ``<root>\\bin`` and touches neither Start Menu nor environment, in either mode."""
    machine = Machine(tmp_path, elevated=elevated)
    machine.app_settings("[integration]\nintegrate = 'none'\n")

    scope = machine.scope()

    assert scope.integrate == "none" and scope.start_menu is None and scope.env_target is None
    assert scope.bin_dir == scope.root / "bin" and scope.path_line() == f'set "PATH={scope.root / "bin"};%PATH%"'


def test_an_elevated_run_in_a_user_writable_gupkg_is_refused_and_reads_nothing(tmp_path: Path) -> None:
    """An administrator must not run code that standard users can change: refused first, before any root or layer is read."""
    machine = Machine(tmp_path, elevated=True, untrusted={tmp_path / "sys" / "opt" / "gupkg"})
    machine.app_settings()

    with pytest.raises(ModeRefused, match="admin-only root") as raised:
        machine.scope()

    assert NOT_ADMIN_ONLY_REFUSAL in str(raised.value) and raised.value.exit_code == 2
    assert "every process is elevated" in str(raised.value) and "only system mode" in str(raised.value)
    assert machine.reads == []


def test_an_elevated_run_refuses_a_user_writable_runtime_inside_an_admin_only_version_directory(tmp_path: Path) -> None:
    """The folder check judges a folder and its ancestors, so the entries an elevated process loads first are named one by one."""
    machine = Machine(tmp_path, elevated=True)
    runtime = machine.location.package_dir / "python"
    runtime.mkdir(parents=True)
    machine.fake.untrusted_dirs.add(runtime)
    machine.app_settings()

    with pytest.raises(ModeRefused, match="admin-only root") as raised:
        machine.scope()

    assert str(runtime) in str(raised.value) and machine.reads == []


def test_an_elevated_run_that_cannot_judge_its_gupkg_tree_is_refused(tmp_path: Path) -> None:
    """A folder that cannot be judged is not trusted."""
    machine = Machine(tmp_path, elevated=True)
    machine.app_settings()

    def broken(path: Path) -> str | None:
        raise PermissionError("denied")

    machine.fake.directory_untrusted_reason = broken  # type: ignore[method-assign]
    machine.fake.directory_is_admin_only = lambda path: False  # type: ignore[method-assign]
    with pytest.raises(ModeRefused, match="admin-only root"):
        machine.scope()
    assert machine.reads == []


def test_a_user_run_reads_nothing_under_the_system_root(tmp_path: Path) -> None:
    """The app layer names both roots, the system root has a settings file, and a user run still never reads under it."""
    machine = Machine(tmp_path)
    machine.app_settings()
    (machine.system_root / ".gupkg").mkdir(parents=True)
    (machine.system_root / ".gupkg" / "settings.toml").write_text('schema = 4\n[ui]\ncolor = "never"\n', encoding="utf-8")
    cli = tmp_path / "cli.toml"
    cli.write_text("schema = 4\n", encoding="utf-8")

    scope = machine.scope(settings_path=cli)

    assert scope.settings["ui.color"] == "auto"
    assert machine.reads, "the user run read nothing at all"
    # The app layer lives beside the system gupkg; only the files of the user run (app layer, user root, --settings) are read.
    assert not [path for path in machine.reads if machine.system_root / ".gupkg" in (path, *path.parents)]


def test_a_system_run_refuses_settings_given_on_the_command_line(tmp_path: Path) -> None:
    """An elevated run reads only admin-only files: ``--settings`` is refused."""
    machine = Machine(tmp_path, elevated=True)
    machine.app_settings()
    cli = tmp_path / "cli.toml"
    cli.write_text("schema = 4\n", encoding="utf-8")

    with pytest.raises(ValueError, match="--settings is not accepted in system mode"):
        machine.scope(settings_path=cli)


def test_the_resolve_text_names_the_mode_the_root_and_its_source(tmp_path: Path) -> None:
    """``<mode> root <path> (<source>)``, the text of the Resolve phase and of the run log."""
    machine = Machine(tmp_path)
    machine.app_settings()

    assert machine.scope().describe() == f"user root {machine.user_root} ({SOURCE_APP})"
    assert machine.scope(root_option=tmp_path / "x").describe() == f"user root {tmp_path / 'x'} (--root)"


@pytest.mark.parametrize(
    ("mode", "supported", "message"),
    [
        pytest.param(Mode.USER, ("system", "user"), None, id="both-in-user"),
        pytest.param(Mode.SYSTEM, ("system", "user"), None, id="both-in-system"),
        pytest.param(Mode.USER, ("user",), None, id="user-only-in-user"),
        pytest.param(Mode.SYSTEM, ("system",), None, id="system-only-in-system"),
        pytest.param(Mode.USER, ("portable",), None, id="portable-in-user"),
        pytest.param(Mode.USER, ("system",), SYSTEM_ONLY_REFUSAL, id="system-only-in-user"),
        pytest.param(Mode.SYSTEM, ("user",), USER_ONLY_REFUSAL, id="user-only-in-system"),
        pytest.param(Mode.SYSTEM, ("portable",), PORTABLE_REFUSAL, id="portable-in-system"),
    ],
)
def test_a_package_is_checked_against_the_mode_of_the_scope(tmp_path: Path, mode: Mode, supported: tuple[str, ...], message: str | None) -> None:
    """``modes`` is compared with ``scope.mode`` before any work, with the message of the case."""
    machine = Machine(tmp_path, elevated=mode is Mode.SYSTEM)
    machine.app_settings()
    scope = machine.scope()
    assert scope.mode is mode

    if message is None:
        check_supported(scope, supported)
    else:
        with pytest.raises(ModeRefused) as raised:
            check_supported(scope, supported)
        assert str(raised.value) == message and raised.value.exit_code == 2


def test_the_system_only_hint_names_the_command_and_the_package_when_the_caller_knows_them(tmp_path: Path) -> None:
    """Q12: ``gupkg <command> <name>`` is filled in when the call site has the command and the package."""
    machine = Machine(tmp_path)
    machine.app_settings()

    with pytest.raises(ModeRefused) as raised:
        check_supported(machine.scope(), ("system",), command="install", name="tool")

    assert str(raised.value).endswith("run in an administrator shell: gupkg install tool")
