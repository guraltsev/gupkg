"""Cover ``gupkg check`` (the health check): drift of integration effects, ``--fix``, shadowing and unverified installs.

Behaviour covered: a deleted shim and a missing ``PATH`` entry are reported and ``--fix`` restores both; a changed shortcut and
a lost environment value are drift too; a dangling ``current`` and a missing ``App`` are reported; unverified installs
(skipped checksum, origin not verified) are listed and a recorded ``verified`` in a user-writable receipt is shown as a
self-report, not proof; shadowing across the system and user roots is reported; an elevated run does not read the user's
environment; a package that is not installed is not unhealthy.

Real: the engine, the catalog and real folders. Mocked: ``FakeSystem``. Out of scope: rendering (issue 029).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gupkg.model.checksum import parse_skip_flag
from gupkg.system.port import Scope
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

FULL = (
    '[[shortcut]]\nname = "Tool"\ntarget = "$App\\\\app.exe"\n\n[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n\n'
    '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n\n[[path]]\nvalue = "$App"\n'
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with tool 1.0.0 installed in the user root."""
    machine = World(tmp_path)
    machine.publish("tool", "1.0.0", tables=FULL)
    assert machine.one("install", "tool").ok
    return machine


def kinds(result) -> list[str]:
    """Return the kinds of the problems of a check result."""
    return [item["kind"] for item in result.details["problems"]]


def test_a_healthy_install_has_no_problems(world: World) -> None:
    """Right after an install nothing is reported as a problem."""
    result = world.one("check", "tool")

    assert result.ok and result.status == "healthy" and result.details["problems"] == []


def test_check_reports_a_deleted_shim_and_a_missing_path_entry_and_fix_restores_both(world: World) -> None:
    """Drift is reported for the shim and the PATH entry; --fix applies both again and the package is healthy."""
    shim = world.fake.scope_folders(Scope.USER).bin_dir / "tool.exe"
    package_dir = str(world.user / "tool" / "current" / "App")
    shim.unlink()
    world.fake.set_path(Scope.USER, [item for item in world.fake.get_path(Scope.USER) if item != package_dir])

    result = world.one("check", "tool")

    assert not result.ok and kinds(result).count("drift") == 2
    messages = " ".join(item["message"] for item in result.details["problems"])
    assert "shim missing" in messages and "PATH entry missing" in messages

    fixed = world.one("check", "tool", fix=True)

    assert fixed.ok and len(fixed.details["fixed"]) == 2
    assert shim.is_file() and package_dir in world.fake.get_path(Scope.USER)
    assert world.one("check", "tool").status == "healthy"


def test_a_changed_shortcut_and_a_lost_environment_value_are_drift_too(world: World) -> None:
    """A shortcut whose target was edited and an environment value that was deleted are reported and repaired."""
    link = next(iter(world.fake.shortcuts()))
    spec = world.fake.read_shortcut(link)
    world.fake.write_shortcut(link, type(spec)(target_path="C:\\elsewhere.exe"))
    world.fake.delete_env(Scope.USER, "TOOL_HOME")

    result = world.one("check", "tool")
    assert kinds(result).count("drift") == 2

    assert world.one("check", "tool", fix=True).ok
    assert world.fake.read_shortcut(link).target_path.endswith("app.exe")
    assert world.fake.env_values(Scope.USER)["TOOL_HOME"].value.endswith("App")


def test_install_again_repairs_drift(world: World) -> None:
    """The install of an installed package re-applies a recorded effect that is missing from the machine."""
    shim = world.fake.scope_folders(Scope.USER).bin_dir / "tool.exe"
    shim.unlink()

    result = world.one("install", "tool")

    assert result.ok and result.status == "repaired" and shim.is_file()


def test_a_dangling_current_and_a_missing_app_are_reported(world: World) -> None:
    """A missing active App is reported as fixable; a current that points nowhere is reported as the package's problem."""
    package = world.user / "tool"
    for item in sorted((package / "1.0.0" / "App").rglob("*"), reverse=True):
        item.unlink()
    (package / "1.0.0" / "App").rmdir()

    result = world.one("check", "tool")

    assert not result.ok and "app-missing" in kinds(result) and result.details["problems"][0]["fixable"] is True


def test_a_package_that_is_not_installed_is_not_unhealthy(world: World) -> None:
    """A local package with no current has nothing to check: it is reported as not installed, healthy."""
    world.publish("other", "1.0.0")

    result = world.one("check", "other")

    assert result.ok and result.status == "not-installed"


def test_a_skipped_checksum_is_listed_as_unverified(tmp_path: Path) -> None:
    """An install that skipped a missing checksum is flagged in check with the way it was allowed."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", hashed=False)
    assert not world.one("install", "tool").ok
    assert world.one("install", "tool", skip=parse_skip_flag("setting")).ok

    result = world.one("check", "tool")

    notes = [note["message"] for note in result.details["notes"] if note["kind"] == "unverified"]
    assert notes == ["installed without a checksum (the manager's checksum_missing = skip)"]


def test_a_stored_verified_is_shown_as_a_self_report_in_user_mode(world: World) -> None:
    """In user mode the receipt can be edited, so a recorded `verified` is reported as the install's own claim."""
    result = world.one("check", "tool")

    assert [note["kind"] for note in result.details["notes"]] == ["self-reported"]
    assert "not proof" in result.details["notes"][0]["message"]


def test_a_changed_app_file_is_reported_whatever_the_receipt_claims(world: World) -> None:
    """A file changed after the install is reported even though the receipt still says `verified`."""
    (world.user / "tool" / "1.0.0" / "App" / "app.exe").write_text("changed", encoding="utf-8")

    result = world.one("check", "tool")

    assert not result.ok and "app-modified" in kinds(result)


def test_shadowing_between_the_system_and_the_user_root_is_reported(tmp_path: Path) -> None:
    """The same package in both roots is a warning of the check: their shims shadow each other on PATH."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", mode="user", tables=FULL)
    world.publish("tool", "1.0.0", mode="system", tables=FULL)
    assert world.one("install", "user:tool").ok

    result = world.one("check", "user:tool")

    assert [note["kind"] for note in result.details["notes"] if note["kind"] == "shadowing"] == ["shadowing"]


def test_an_elevated_check_does_not_read_the_users_environment(tmp_path: Path) -> None:
    """An elevated run verifies system-scope effects only and never asks the user's environment."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system", tables=FULL)
    assert world.one("install", "system:tool").ok
    world.fake.registry_log.clear()

    result = world.one("check", "tool")

    assert result.ok and {access.scope for access in world.fake.registry_log} == {"system"}
