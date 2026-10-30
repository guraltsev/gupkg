"""Cover the reports of a failed step (issue 035, item 9): what happened, what state the package is in, and the command that fixes it.

Behaviour covered: ``uninstall --purge`` with a file that another program holds open names the file, says the package is already
uninstalled and its folder left, and gives the command to run again (which then succeeds); an ``update`` that fails after ``current``
moved says which step failed and what ``current`` points to; ``check`` and ``check --fix`` report a package that has versions and a
shim but no ``current``, and a package with nothing left stays ``not-installed``; ``check --fix`` of a missing shipped App says how to
reinstall; ``self status`` reports a gupkg package without ``current``.

Real: the engine, the catalog, real folders and junctions, and (on Windows) a file held open with no sharing. Mocked: ``FakeSystem``.
Out of scope: rendering.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from gupkg.selfmgmt.location import RunningGupkg
from gupkg.system.port import Mode
from tests.cli_support import CliWorld
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

SHIM = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="holds a file open with no sharing (Windows file locking)")


@contextlib.contextmanager
def held_open(path: Path) -> Iterator[None]:
    """Hold *path* open with no sharing (like a running program): it cannot be deleted or read by others until the block ends."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "cannot hold the file open", str(path))
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with the shipped package tool 1.0.0 (one shim) installed in the user root."""
    machine = World(tmp_path)
    machine.publish_shipped("tool", "1.0.0", tables=SHIM)
    assert machine.one("install", "tool").ok
    return machine


def shim_file(world: World) -> Path:
    """Return the shim file of tool."""
    return world.fake.scope_folders(Mode.USER).bin_dir / "tool.exe"


# -- 1. uninstall --purge with a locked file --------------------------------------------------------------------------


@windows_only
def test_a_purge_that_meets_a_file_in_use_says_what_is_left_and_the_command_to_run_again(world: World) -> None:
    """The failure names the file as in use, says the package is uninstalled and its folder remains, names the command; the retry works."""
    package = world.user / "tool"
    locked = package / "1.0.0" / "App" / "app.exe"
    world.engine.yes = True

    with held_open(locked):
        failed = world.one("uninstall", "tool", purge=True)

    assert not failed.ok and failed.exit_code == 3
    message = failed.error["message"]
    assert f"{locked} is in use by another program" in message
    assert "The package is uninstalled" in message and "`current` are removed" in message and "still there" in message
    assert "close the program, then run `gupkg uninstall tool --purge` again" in message
    assert package.is_dir() and not (package / "current").exists()

    retry = world.one("uninstall", "tool", purge=True)

    assert retry.ok and retry.status == "purged" and not package.exists()


# -- 2. update that fails after current moved -------------------------------------------------------------------------


@windows_only
def test_an_update_that_fails_after_current_moved_says_which_step_failed_and_where_current_points(world: World) -> None:
    """The receipt of the new version cannot read its locked App: the failure names the step and says the package is now on 1.1.0."""
    new = world.publish_shipped("tool", "1.1.0", tables=SHIM)

    with held_open(new / "App" / "app.exe"):
        failed = world.one("update", "tool")

    assert not failed.ok and failed.exit_code == 3
    message = failed.error["message"]
    assert message.startswith("the Finish step failed:")
    assert "The package is now on 1.1.0: `current` points to 1.1.0; version 1.0.0 is kept." in message
    assert "`gupkg check tool` shows its state" in message and "`gupkg switch tool 1.0.0` returns to the old version" in message
    assert Path(os.path.realpath(world.user / "tool" / "current")).name == "1.1.0" and shim_file(world).is_file()


def test_a_failure_before_current_moves_keeps_its_own_message(world: World) -> None:
    """An update that fails before the switch is not rewritten: it does not claim that the package is on the new version."""
    world.publish("tool", "1.1.0", tables=SHIM, hashed=False, source='sha256 = "' + "0" * 64 + '"')

    failed = world.one("update", "tool")

    assert not failed.ok
    assert "The package is now on" not in failed.error["message"]
    assert Path(os.path.realpath(world.user / "tool" / "current")).name == "1.0.0"


# -- 3. check with current missing ------------------------------------------------------------------------------------


def test_check_reports_a_package_whose_current_is_missing_and_check_fix_changes_nothing(world: World) -> None:
    """A package with a version and a shim but no `current` is a problem of that package, with the command that fixes it."""
    package = world.user / "tool"
    os.rmdir(package / "current")

    for fix in (False, True):
        result = world.one("check", "tool", fix=fix)

        assert not result.ok and result.status == "unhealthy" and result.exit_code == 3
        kinds = [item["kind"] for item in result.details["problems"]]
        assert kinds == ["current-missing"]
        message = result.error["message"]
        assert "tool has versions 1.0.0 but no active version (`current` is missing)" in message
        assert "the shim `tool.exe` is still in `bin`" in message
        assert "`gupkg install tool`" in message
        assert shim_file(world).is_file() and not (package / "current").exists()

    assert world.one("install", "tool").ok
    assert world.one("check", "tool").status == "healthy"


def test_a_package_with_versions_and_no_integration_left_stays_not_installed(world: World) -> None:
    """After a plain uninstall (no shim, no `current`) check says not-installed, as before."""
    assert world.one("uninstall", "tool").ok

    result = world.one("check", "tool")

    assert result.ok and result.status == "not-installed" and result.details["problems"] == []


# -- 4. self status without current -----------------------------------------------------------------------------------


def test_self_status_says_that_the_gupkg_package_has_no_active_version(tmp_path: Path) -> None:
    """An installed gupkg whose package folder has no `current` reports it, with the command; with `current` there is no such line."""
    world = CliWorld(tmp_path)
    package = world.location.version_dir.parent
    assert not (package / "current").exists()

    missing = world.cli("self", "status", "--json").document()

    texts = [problem for problem in missing["problems"] if "no active version" in problem]
    assert len(texts) == 1 and "`current` is missing" in texts[0] and "`gupkg install gupkg`" in texts[0]
    assert missing["ok"] is False

    world.fake.create_junction(package / "current", world.location.version_dir)

    present = world.cli("self", "status", "--json").document()

    assert not any("no active version" in problem for problem in present["problems"])


def test_self_status_of_a_source_checkout_does_not_look_for_current(tmp_path: Path) -> None:
    """A source or bundled gupkg is not a package of a root, so `current` is not asked about."""
    world = CliWorld(tmp_path, installed=False)

    document = world.cli("self", "status", "--json").document()

    assert not any("no active version" in problem for problem in document["problems"])
    assert isinstance(world.location, RunningGupkg) and world.location.kind == "source"


# -- 5. check --fix with a missing shipped App ------------------------------------------------------------------------


def test_check_fix_of_a_missing_shipped_app_says_how_to_restore_it(world: World) -> None:
    """With the App gone and the shim deleted, --fix cannot rebuild a shipped App: the line says so and names the reinstall command."""
    import shutil

    shutil.rmtree(world.user / "tool" / "1.0.0" / "App")
    shim_file(world).unlink()

    result = world.one("check", "tool", fix=True)

    assert not result.ok and result.exit_code == 2
    assert result.error["message"] == (
        "App is missing and no way to recover it: its files ship with the version. Restore the App folder of 1.0.0 "
        "(copy the package files back), or install another version; `gupkg install tool` cannot rebuild a shipped App."
    )
    assert "Reinstall with" not in result.error["message"]
