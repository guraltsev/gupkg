"""Cover the trust rules that gate elevated gupkg runs.

Behavior covered: which directories an elevated run accepts or refuses, that the
trust check covers the whole active version directory (including the bundled
runtime and its dependency folder), that the bootstrap refuses any run on
a non-bundled interpreter, and that an elevated update relaunch goes through the
isolated launcher.

Mocked boundaries: the Windows security-descriptor reader (replaced by synthetic
descriptors), the process's elevation state, and the Windows shell relaunch call.
One Windows-only test reads real access-control
lists from system folders and the temporary folder. Out of scope: running a real
elevated process, which needs an administrator token.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from gupkg import elevation
from gupkg.elevation import AccessEntry, DirectorySecurity, UntrustedLocationError

ADMINS = "S-1-5-32-544"
SYSTEM = "S-1-5-18"
USERS = "S-1-5-32-545"
AUTHENTICATED = "S-1-5-11"
SOME_USER = "S-1-5-21-1-2-3-1001"
MODIFY = 0x1301BF
READ_EXECUTE = 0x1200A9
FULL = 0x1F01FF
DELETE_CHILD_ONLY = 0x40

SRC = Path(__file__).resolve().parents[1] / "src" / "gupkg"


def _security(*entries: tuple[str, int], owner: str = ADMINS, inherit_only: bool = False) -> DirectorySecurity:
    """Build a synthetic descriptor from (sid, mask) pairs."""
    return DirectorySecurity(owner, tuple(AccessEntry(sid, mask, inherit_only) for sid, mask in entries))


def _reader(table: dict[str, DirectorySecurity]):
    """Return a reader that serves descriptors by path and defaults to admin-only."""
    admin_only = _security((SYSTEM, FULL), (ADMINS, FULL), (USERS, READ_EXECUTE))

    def read(path: Path) -> DirectorySecurity:
        return table.get(str(path), admin_only)

    return read


def test_admin_only_directory_with_read_access_for_users_is_trusted(tmp_path: Path) -> None:
    """A directory only administrators can modify passes the trusted-location check."""
    elevation.require_trusted_directory(tmp_path, reader=_reader({}))


@pytest.mark.parametrize(
    ("sid", "mask", "named"),
    [
        pytest.param(USERS, MODIFY, "Users", id="users-modify"),
        pytest.param(AUTHENTICATED, MODIFY, "Authenticated Users", id="authenticated-users-modify"),
        pytest.param(SOME_USER, FULL, SOME_USER, id="single-user-full-control"),
        pytest.param(USERS, 0x40000, "Users", id="users-may-change-permissions"),
    ],
)
def test_directory_writable_by_non_administrators_is_refused(tmp_path: Path, sid: str, mask: int, named: str) -> None:
    """A directory any non-administrator can modify is refused, naming the principal and a fix."""
    table = {str(tmp_path.resolve()): _security((SYSTEM, FULL), (ADMINS, FULL), (sid, mask))}

    with pytest.raises(UntrustedLocationError) as caught:
        elevation.require_trusted_directory(tmp_path, reader=_reader(table))

    message = str(caught.value)
    assert named in message
    assert str(tmp_path.resolve()) in message
    assert "icacls" in message


def test_directory_owned_by_a_standard_user_is_refused(tmp_path: Path) -> None:
    """The owner can rewrite permissions, so a user-owned directory is not trusted."""
    table = {str(tmp_path.resolve()): _security((SYSTEM, FULL), (ADMINS, FULL), owner=SOME_USER)}

    with pytest.raises(UntrustedLocationError, match="owned by"):
        elevation.require_trusted_directory(tmp_path, reader=_reader(table))


def test_directory_without_an_access_list_is_refused(tmp_path: Path) -> None:
    """A descriptor with no access list grants everyone access and is refused."""
    table = {str(tmp_path.resolve()): DirectorySecurity(ADMINS, (), has_dacl=False)}

    with pytest.raises(UntrustedLocationError, match="no access-control list"):
        elevation.require_trusted_directory(tmp_path, reader=_reader(table))


def test_unreadable_permissions_fail_closed(tmp_path: Path) -> None:
    """When a descriptor cannot be read the directory is refused, not assumed safe."""

    def failing(path: Path) -> DirectorySecurity:
        raise OSError("access denied")

    with pytest.raises(UntrustedLocationError, match="could not be read"):
        elevation.require_trusted_directory(tmp_path, reader=failing)


def test_ancestor_that_lets_users_replace_the_directory_is_refused(tmp_path: Path) -> None:
    """A user who can delete children of a parent can swap the directory for a fake one."""
    child = tmp_path / "gupkg"
    child.mkdir()
    table = {
        str(tmp_path.resolve()): _security((SYSTEM, FULL), (ADMINS, FULL), (USERS, DELETE_CHILD_ONLY)),
    }

    with pytest.raises(UntrustedLocationError, match="parent"):
        elevation.require_trusted_directory(child, reader=_reader(table))


def test_inheritance_only_entries_do_not_count_against_the_directory(tmp_path: Path) -> None:
    """Entries that apply only to future children do not make the directory itself writable."""
    table = {
        str(tmp_path.resolve()): DirectorySecurity(
            ADMINS,
            (
                AccessEntry(ADMINS, FULL),
                AccessEntry(USERS, MODIFY, inherit_only=True),
            ),
        )
    }

    elevation.require_trusted_directory(tmp_path, reader=_reader(table))


@pytest.mark.skipif(os.name != "nt", reason="reads Windows access-control lists")
def test_real_system_folder_is_trusted_and_user_temp_folder_is_not() -> None:
    """Real access-control lists classify a system folder as admin-only and the temp folder as not."""
    system_folder = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    elevation.require_trusted_directory(system_folder)

    with pytest.raises(UntrustedLocationError):
        elevation.require_trusted_directory(Path(os.environ["TEMP"]))


@pytest.mark.parametrize("elevated", [False, True], ids=["ordinary", "elevated"])
def test_runtime_refuses_an_interpreter_that_is_not_the_bundled_one(tmp_path: Path, elevated: bool) -> None:
    """A run on any interpreter other than the package's bundled Python is refused, elevated or not."""
    package = tmp_path / "gupkg"
    (package / "python").mkdir(parents=True)
    (package / "python" / "python.exe").touch()

    with pytest.raises(UntrustedLocationError, match="runs only on its bundled Python"):
        elevation.enforce_bundled_runtime(package, elevated=elevated)


def test_runtime_refuses_the_bundled_interpreter_started_without_isolation(tmp_path: Path) -> None:
    """The bundled python.exe started without -I (so PYTHON* variables count) is refused even for an ordinary run."""
    package = tmp_path / "gupkg"
    (package / "python").mkdir(parents=True)
    python = package / "python" / "python.exe"
    python.touch()

    with (
        mock.patch.object(sys, "executable", str(python)),
        mock.patch.object(sys, "flags", types.SimpleNamespace(isolated=0)),
        pytest.raises(UntrustedLocationError, match="runs only on its bundled Python"),
    ):
        elevation.enforce_bundled_runtime(package)


def test_ordinary_run_on_the_isolated_bundled_interpreter_needs_no_admin_only_folders(tmp_path: Path) -> None:
    """An ordinary run passes on the isolated bundled Python without reading any folder's access-control list."""
    package = tmp_path / "gupkg"
    (package / "python").mkdir(parents=True)
    python = package / "python" / "python.exe"
    python.touch()

    with (
        mock.patch.object(sys, "executable", str(python)),
        mock.patch.object(sys, "flags", types.SimpleNamespace(isolated=1)),
        mock.patch.object(elevation, "require_trusted_directory") as trusted,
    ):
        elevation.enforce_bundled_runtime(package)

    trusted.assert_not_called()


def test_bootstrap_refuses_a_run_on_the_wrong_interpreter_without_importing_the_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The bootstrap exits 1 with the bundled-Python message instead of running the CLI when the interpreter is not the bundled one."""
    spec = importlib.util.spec_from_file_location("bootstrap_elevated_under_test", SRC / "bootstrap.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    package = tmp_path / "gupkg"
    (package / "python").mkdir(parents=True)
    (package / "bootstrap.py").touch()
    cli = types.ModuleType("gupkg.cli")
    cli.main = mock.Mock(return_value=0)

    with (
        mock.patch.object(bootstrap, "__file__", str(package / "bootstrap.py")),
        mock.patch.dict(sys.modules, {"gupkg.cli": cli}),
    ):
        status = bootstrap.main(["list"])

    assert status == 1
    assert "bundled Python" in capsys.readouterr().err
    cli.main.assert_not_called()


# ---------------------------------------------------------------------------
# The bundled runtime under elevation
# ---------------------------------------------------------------------------


def test_elevated_trust_check_covers_the_version_directory_and_the_whole_bundled_runtime(tmp_path: Path) -> None:
    """An elevated run on the bundled Python demands admin-only access to the version folder, python\\ and its site-packages."""
    version_root = tmp_path / "0.18.0"
    package = version_root / "gupkg"
    site_packages = package / "python" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    python = package / "python" / "python.exe"
    python.touch()
    isolated = types.SimpleNamespace(isolated=1)

    with (
        mock.patch.object(sys, "executable", str(python)),
        mock.patch.object(sys, "flags", isolated),
        mock.patch.object(elevation, "require_trusted_directory") as trusted,
    ):
        elevation.enforce_bundled_runtime(package, elevated=True)

    checked = {Path(call.args[0]).resolve() for call in trusted.call_args_list}
    assert {version_root.resolve(), package.resolve(), (package / "python").resolve(), site_packages.resolve()} <= checked


# ---------------------------------------------------------------------------
# Elevated relaunch
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="uses the Windows shell relaunch")
def test_elevated_relaunch_goes_through_the_isolated_launcher() -> None:
    """The elevated update relaunch starts gupkg.cmd, never `python -m gupkg` in the caller's directory."""
    from gupkg import windows

    shell_execute = mock.Mock(return_value=42)
    fake_windll = types.SimpleNamespace(shell32=types.SimpleNamespace(ShellExecuteW=shell_execute))

    with mock.patch("ctypes.windll", fake_windll, create=True):
        accepted = windows.relaunch_elevated(["manager", "update", "--yes"])

    assert accepted is True
    _, verb, program, parameters, directory, _ = shell_execute.call_args.args
    assert verb == "runas"
    assert Path(program).name.lower() == "cmd.exe"
    assert "gupkg.cmd" in parameters
    assert "manager update --yes" in parameters
    assert "-m gupkg" not in parameters
    assert Path(directory) == SRC
