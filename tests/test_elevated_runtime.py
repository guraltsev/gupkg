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


@pytest.mark.parametrize(
    "flags",
    [types.SimpleNamespace(isolated=0, no_site=1), types.SimpleNamespace(isolated=1, no_site=0)],
    ids=["without -I", "without -S"],
)
def test_runtime_refuses_the_bundled_interpreter_started_without_isolation_or_with_site(tmp_path: Path, flags) -> None:
    """The bundled python.exe started without -I (PYTHON* variables count) or without -S (site, sitecustomize and .pth run) is refused."""
    package = tmp_path / "gupkg"
    (package / "python").mkdir(parents=True)
    python = package / "python" / "python.exe"
    python.touch()

    with (
        mock.patch.object(sys, "executable", str(python)),
        mock.patch.object(sys, "flags", flags),
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
        mock.patch.object(sys, "flags", types.SimpleNamespace(isolated=1, no_site=1)),
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
    """An elevated run on the bundled Python demands admin-only access to the version folder, python\\ and its site-packages, python.exe and the ._pth file."""
    version_root = tmp_path / "0.18.0"
    package = version_root / "gupkg"
    site_packages = package / "python" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    python = package / "python" / "python.exe"
    python.touch()
    search_path_file = package / "python" / "python312._pth"
    search_path_file.touch()
    isolated = types.SimpleNamespace(isolated=1, no_site=1)

    with (
        mock.patch.object(sys, "executable", str(python)),
        mock.patch.object(sys, "flags", isolated),
        mock.patch.object(elevation, "require_trusted_directory") as trusted,
    ):
        elevation.enforce_bundled_runtime(package, elevated=True)

    checked = {Path(call.args[0]).resolve() for call in trusted.call_args_list}
    assert {version_root.resolve(), package.resolve(), (package / "python").resolve(), site_packages.resolve()} <= checked
    assert {python.resolve(), search_path_file.resolve()} <= checked


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


# ---------------------------------------------------------------------------
# Elevation detection fails closed; checks run before the import path changes
# ---------------------------------------------------------------------------

windows_only = pytest.mark.skipif(os.name != "nt", reason="elevation signals exist only on Windows")


def _detect(admin, token) -> bool:
    """Run is_elevated() with the two Windows signals replaced by *admin* and *token* (a bool or an exception)."""

    def signal(value):
        def read():
            if isinstance(value, Exception):
                raise value
            return value

        return read

    with (
        mock.patch.object(elevation, "_admin_signal", signal(admin)),
        mock.patch.object(elevation, "_token_elevation_signal", signal(token)),
    ):
        return elevation.is_elevated()


@windows_only
@pytest.mark.parametrize(
    ("admin", "token", "expected"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
        (OSError("no shell32"), False, True),
        (False, OSError("no token"), True),
    ],
    ids=["neither", "IsUserAnAdmin-only", "TokenElevation-only", "admin-signal-fails", "token-signal-fails"],
)
def test_elevation_is_true_when_either_signal_says_so_or_cannot_be_read(admin, token, expected) -> None:
    """Either Windows signal reporting elevation, or failing to report at all, counts as elevated; only two clean 'no' answers do not."""
    assert _detect(admin, token) is expected


@windows_only
def test_real_token_signals_can_be_read_without_error() -> None:
    """Both real Windows signals answer with a bool for this process, so detection does not fall back to 'cannot tell'."""
    assert isinstance(elevation._admin_signal(), bool)
    assert isinstance(elevation._token_elevation_signal(), bool)


def _bootstrap_module():
    """Load a fresh copy of the bootstrap script as a module."""
    spec = importlib.util.spec_from_file_location("bootstrap_detection_under_test", SRC / "bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundled_package(tmp_path: Path) -> tuple[Path, Path]:
    """Create ``<tmp>/gupkg/python/python.exe`` and return the package and interpreter paths."""
    package = tmp_path / "gupkg"
    (package / "python").mkdir(parents=True)
    (package / "bootstrap.py").touch()
    python = package / "python" / "python.exe"
    python.touch()
    return package, python


@pytest.mark.parametrize(
    ("env_marker", "admin_signal", "expect_trust_check"),
    [("1", False, True), (None, True, True), (None, False, False)],
    ids=["GUPKG_ELEVATED=1", "elevation-signal", "ordinary"],
)
def test_bootstrap_applies_the_admin_only_checks_exactly_for_elevated_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_marker, admin_signal, expect_trust_check
) -> None:
    """On the bundled isolated Python the bootstrap checks directories when GUPKG_ELEVATED=1 or a signal says elevated, and not for an ordinary run."""
    package, python = _bundled_package(tmp_path)
    bootstrap = _bootstrap_module()
    cli = types.ModuleType("gupkg.cli")
    cli.main = mock.Mock(return_value=0)
    if env_marker:
        monkeypatch.setenv("GUPKG_ELEVATED", env_marker)
    else:
        monkeypatch.delenv("GUPKG_ELEVATED", raising=False)
    refuse = UntrustedLocationError("writable")

    with (
        mock.patch.object(bootstrap, "__file__", str(package / "bootstrap.py")),
        mock.patch.object(bootstrap, "_load_elevation", return_value=elevation),
        mock.patch.object(sys, "executable", str(python)),
        mock.patch.object(sys, "flags", types.SimpleNamespace(isolated=1, no_site=1)),
        mock.patch.object(elevation, "is_elevated", return_value=admin_signal),
        mock.patch.object(elevation, "require_trusted_directory", side_effect=refuse) as trusted,
        mock.patch.dict(sys.modules, {"gupkg.cli": cli}),
    ):
        status = bootstrap.main(["list"])

    assert trusted.called is expect_trust_check
    assert status == (1 if expect_trust_check else 0)
    assert cli.main.called is (not expect_trust_check)


def test_bootstrap_adds_nothing_to_sys_path_until_the_checks_pass(tmp_path: Path) -> None:
    """The package parent is not on sys.path while the trust check runs, and is not added at all when the check refuses."""
    package, _ = _bundled_package(tmp_path)
    bootstrap = _bootstrap_module()
    seen: list[list[str]] = []

    def refuse(*args, **kwargs) -> None:
        seen.append(list(sys.path))
        raise UntrustedLocationError("no")

    fake = types.SimpleNamespace(
        UntrustedLocationError=UntrustedLocationError, elevated_mode=lambda: True, enforce_bundled_runtime=refuse
    )
    before = list(sys.path)

    with (
        mock.patch.object(bootstrap, "__file__", str(package / "bootstrap.py")),
        mock.patch.object(bootstrap, "_load_elevation", return_value=fake),
    ):
        status = bootstrap.main(["list"])

    assert status == 1
    assert seen == [before]
    assert sys.path == before


def test_bootstrap_reports_a_runtime_without_ctypes_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """If a module the check needs cannot be imported, the bootstrap prints a reinstall message and exits 1 instead of a traceback."""
    package, _ = _bundled_package(tmp_path)
    bootstrap = _bootstrap_module()
    cli = types.ModuleType("gupkg.cli")
    cli.main = mock.Mock(return_value=0)

    with (
        mock.patch.object(bootstrap, "__file__", str(package / "bootstrap.py")),
        mock.patch.object(bootstrap, "_load_elevation", side_effect=ImportError("No module named 'ctypes'")),
        mock.patch.dict(sys.modules, {"gupkg.cli": cli}),
    ):
        status = bootstrap.main(["list"])

    assert status == 1
    assert "Reinstall gupkg from a release ZIP" in capsys.readouterr().err
    cli.main.assert_not_called()
