"""Cover the per-package lock: a second run is refused, a stale lock is reclaimed, cleanup leaves a run in flight alone.

Behaviour covered: an install, update, uninstall or ``check --fix`` of a package held by a living run fails with exit
status 3 naming the holder and changes nothing, and the holder's lock stays; ``check`` and ``list`` read only and take none;
a lock whose process is gone, one older than a day and an unreadable old one are reclaimed; an unreadable brand-new one and
one of another host with a living-looking age are respected; ``cleanup`` skips a locked package and never removes a work
entry younger than a day even when nothing holds the lock; the lock is released after a success, a failure, an exception
and a cancel, and a first install that fails leaves no package folder behind; an elevated run refuses a locks folder
that is not admin-only.

Real: the engine, the lock files and real folders. Mocked: ``FakeSystem`` (process liveness, elevation, untrusted folders).
Out of scope: the command-line wording (issue 029).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gupkg.engine.context import stop_marker
from gupkg.engine.locks import Holder, PackageBusy, lock_holder, lock_path, package_lock, render_lock
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

ENV_ONLY = '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'
OTHER_PID = 424242


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with tool 1.0.0 installed, and 1.1.0 published."""
    machine = World(tmp_path)
    machine.publish("tool", "1.0.0", tables=ENV_ONLY)
    assert machine.one("install", "tool").ok
    machine.publish("tool", "1.1.0", tables=ENV_ONLY)
    return machine


def hold(world: World, package: Path, *, pid: int = OTHER_PID, host: str | None = None, age_days: float = 0.0, live: bool = True,
         text: str | None = None) -> Path:
    """Write a lock file as another run would have, optionally aged, with a living or dead process."""
    path = lock_path(package)
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = Holder(pid, host or socket.gethostname(), "install", "2026-10-24T10:00:00", "othertoken")
    path.write_text(text if text is not None else render_lock(holder), encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 24 * 3600
        os.utime(path, (old, old))
    if live:
        world.fake.live_pids.add(pid)
    return path


def test_a_second_install_is_refused_naming_the_holder_and_the_holders_lock_stays(world: World) -> None:
    """Exit status 3, the message names the process, the operation and the host; nothing changes and the lock is not removed."""
    package = world.user / "tool"
    lock = hold(world, package)
    before = (package / "current").resolve()

    result = world.one("update", "tool")

    assert not result.ok and result.exit_code == 3 and result.error["code"] == "package-busy"
    assert str(OTHER_PID) in result.error["message"] and "install" in result.error["message"] and result.error.get("hint")
    assert (package / "current").resolve() == before and "othertoken" in lock.read_text(encoding="utf-8")


@pytest.mark.parametrize("operation", ["install", "uninstall", "cleanup", "check-updates"])
def test_every_operation_that_changes_a_package_takes_the_lock(world: World, operation: str) -> None:
    """Install, uninstall, cleanup and check-updates see a living holder (cleanup is skipped, the others fail)."""
    hold(world, world.user / "tool")

    result = world.one(operation, "tool")

    if operation == "cleanup":
        assert result.ok and result.status == "skipped" and result.details["locked"] is True
    else:
        assert not result.ok and result.error["code"] == "package-busy"


def test_check_reads_only_but_check_fix_takes_the_lock(world: World) -> None:
    """A plain check works while another run holds the package; --fix is refused."""
    hold(world, world.user / "tool")

    assert world.one("check", "tool").ok
    refused = world.one("check", "tool", fix=True)

    assert not refused.ok and refused.error["code"] == "package-busy"


def test_a_lock_of_a_dead_process_is_reclaimed_and_released_after_the_run(world: World) -> None:
    """The stale lock is replaced, the run succeeds and no lock file or folder is left."""
    package = world.user / "tool"
    hold(world, package, live=False)

    result = world.one("update", "tool")

    assert result.ok and not lock_path(package).exists() and not lock_path(package).parent.exists()


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        pytest.param({"age_days": 2}, None, id="older-than-a-day-though-the-process-lives"),
        pytest.param({"age_days": 2, "host": "elsewhere"}, None, id="old-lock-of-another-host"),
        pytest.param({"age_days": 1, "live": False}, "garbage = [", id="unreadable-and-old"),
    ],
)
def test_an_old_or_unreadable_old_lock_is_reclaimed(world: World, kwargs: dict, text: str | None) -> None:
    """A recycled process id cannot hold a lock for ever; an old unreadable file is no lock."""
    package = world.user / "tool"
    hold(world, package, text=text, **kwargs)

    assert world.one("update", "tool").ok


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        pytest.param({"host": "elsewhere", "live": False}, None, id="another-host-judged-by-age-only"),
        pytest.param({"live": False}, "garbage = [", id="unreadable-but-brand-new"),
    ],
)
def test_a_lock_that_cannot_be_judged_dead_is_respected(world: World, kwargs: dict, text: str | None) -> None:
    """A lock of another host or one still being written is not stolen."""
    hold(world, world.user / "tool", text=text, **kwargs)

    result = world.one("update", "tool")

    assert not result.ok and result.error["code"] == "package-busy"


def test_cleanup_never_removes_a_work_folder_younger_than_a_day_even_when_unlocked(world: World) -> None:
    """The in-flight `populate-...` staging folder survives a cleanup; an old one goes."""
    package = world.user / "tool"
    young = package / ".gupkg" / "work" / "populate-young"
    young.mkdir(parents=True)
    (young / "half.bin").write_text("x", encoding="utf-8")
    old = package / ".gupkg" / "work" / "populate-old"
    old.mkdir(parents=True)
    ancient = time.time() - 3 * 24 * 3600
    os.utime(old, (ancient, ancient))

    result = world.one("cleanup", "tool")

    assert result.ok and result.details["work"] == ["populate-old"] and young.is_dir() and not old.exists()


def test_cleanup_of_a_locked_package_removes_nothing(world: World) -> None:
    """A package another run is changing is skipped whole, even its old receipts and work entries."""
    package = world.user / "tool"
    work = package / ".gupkg" / "work" / "populate-inflight"
    work.mkdir(parents=True)
    ancient = time.time() - 3 * 24 * 3600
    os.utime(work, (ancient, ancient))
    hold(world, package)

    result = world.one("cleanup", "tool")

    assert result.status == "skipped" and work.is_dir()


def test_the_lock_is_released_after_a_failure_an_exception_and_a_cancel(world: World, tmp_path: Path) -> None:
    """No path leaves the lock behind: a refused download, an exception inside the block, and a cancelled batch."""
    package = world.user / "tool"
    world.publish("other", "1.0.0", hashed=False)
    refused = world.one("install", "other")
    assert not refused.ok and not lock_path(world.user / "other").exists()

    with pytest.raises(RuntimeError):
        with package_lock(world.fake, package, "install", world.engine.now):
            assert lock_path(package).is_file()
            raise RuntimeError("boom")
    assert not lock_path(package).exists()

    world.publish("one", "1.0.0")
    world.publish("two", "1.0.0")
    marker = tmp_path / "stop"
    world.engine.cancel = stop_marker(marker)
    calls = []
    original = world.engine.cancel

    def cancel_after_first() -> bool:
        calls.append(1)
        if len(calls) == 2:
            marker.write_text("", encoding="utf-8")
        return original()

    world.engine.cancel = cancel_after_first
    batch = world.run("install", "one", "two")
    assert [item.status for item in batch.results][1] == "skipped"
    assert not lock_path(world.user / "one").exists() and not lock_path(world.user / "two").exists()


def test_a_package_folder_made_for_the_lock_is_removed_when_nothing_came_to_live_in_it(world: World) -> None:
    """The lock creates the folder of a package that does not exist yet; with nothing written in it, it is gone again."""
    fresh = world.user / "fresh"

    with pytest.raises(RuntimeError):
        with package_lock(world.fake, fresh, "install", world.engine.now):
            assert lock_path(fresh).is_file()
            raise RuntimeError("the install failed before it wrote anything")
    assert not fresh.exists()

    with package_lock(world.fake, fresh, "install", world.engine.now):
        (fresh / "1.0.0").mkdir()
    assert (fresh / "1.0.0").is_dir() and not lock_path(fresh).parent.exists()


def test_a_refused_first_install_from_a_distribution_leaves_no_lock_behind(world: World) -> None:
    """A distribution install that the checksum rules refuse (D46) releases its lock, in a package that did not exist."""
    world.publish("fresh", "1.0.0", hashed=False, root=world.base / "downloads")

    result = world.one("install", str(world.base / "downloads" / "fresh" / "1.0.0"))

    assert not result.ok and not lock_path(world.user / "fresh").exists()

def test_the_lock_holder_is_visible_without_taking_the_lock(world: World) -> None:
    """lock_holder reports a live holder and ignores a stale one; it never changes a file."""
    package = world.user / "tool"
    assert lock_holder(world.fake, package, world.engine.now()) is None
    path = hold(world, package)
    before = path.read_bytes()

    holder = lock_holder(world.fake, package, world.engine.now())

    assert holder is not None and holder.pid == OTHER_PID and path.read_bytes() == before
    world.fake.live_pids.clear()
    assert lock_holder(world.fake, package, world.engine.now()) is None


def test_package_lock_refuses_a_live_holder_directly(world: World) -> None:
    """The context manager itself raises PackageBusy with the holder named."""
    package = world.user / "tool"
    hold(world, package)

    with pytest.raises(PackageBusy, match=str(OTHER_PID)):
        with package_lock(world.fake, package, "install", world.engine.now):
            pytest.fail("the lock was taken")


def test_an_elevated_run_refuses_a_locks_folder_that_is_not_admin_only(tmp_path: Path) -> None:
    """A standard user's writable locks folder cannot hold the lock of an administrator's run."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system", tables=ENV_ONLY)
    world.fake.untrusted_dirs = {Path(os.path.abspath(world.system_root / "tool" / ".gupkg" / "locks"))}

    with pytest.raises(PackageBusy, match="admin-only"):
        with package_lock(world.fake, world.system_root / "tool", "install", world.engine.now, elevated=True):
            pytest.fail("the lock was taken")
    result = world.one("install", "system:tool")

    assert not result.ok and result.error["code"] == "package-busy"


def test_an_elevated_install_locks_inside_the_system_package_and_never_writes_the_users_root(tmp_path: Path) -> None:
    """While the administrator's install runs its lock file is below the system package; nothing is written in the user's root."""
    world = World(tmp_path, elevated=True)
    world.publish("tool", "1.0.0", mode="system", tables=ENV_ONLY)
    seen: list[Path] = []

    def clock():
        seen.extend(world.base.rglob("package.lock"))
        return datetime.now(timezone.utc)

    world.engine.clock = clock

    result = world.one("install", "system:tool")

    assert result.ok and seen and {item.parent.parent.parent for item in seen} == {world.system_root / "tool"}
    assert not any("tool" in item.name for item in world.user.rglob("*"))
    assert not lock_path(world.system_root / "tool").exists()


def test_racing_runs_never_hold_the_lock_together(world: World) -> None:
    """Sixteen threads ask for one package's lock at once: at no moment do two of them hold it."""
    package = world.user / "tool"
    inside = [0]
    most = [0]
    guard = threading.Lock()
    refused = [0]

    def run() -> None:
        try:
            with package_lock(world.fake, package, "install", world.engine.now):
                with guard:
                    inside[0] += 1
                    most[0] = max(most[0], inside[0])
                time.sleep(0.02)
                with guard:
                    inside[0] -= 1
        except PackageBusy:
            with guard:
                refused[0] += 1

    threads = [threading.Thread(target=run) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert most[0] == 1 and refused[0] >= 1 and not lock_path(package).exists()
