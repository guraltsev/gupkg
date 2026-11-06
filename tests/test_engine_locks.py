"""Cover the per-package lock: a second run is refused, a stale lock is reclaimed, cleanup leaves a run in flight alone.

Behaviour covered: an install, update, uninstall or ``check --fix`` of a package held by a living run fails with exit
status 3 naming the holder and changes nothing, and the holder's lock stays; ``check`` and ``list`` only observe and take none;
a lock whose process is gone, one older than a day and an unreadable old one are reclaimed; an unreadable brand-new one and
one of another host with a living-looking age are respected; ``cleanup`` of a package the user NAMES that is locked fails
with ``package-busy`` (exit 3), while a root-wide sweep (``sweep=True``) leaves it alone as ``locked`` without failing, and
neither removes a work entry; ``cleanup`` never removes a work entry younger than a day even when nothing holds the lock; the lock is released after a success, a failure, an exception
and a cancel, and a first install that fails leaves no package folder behind; an elevated run refuses a locks folder
that is not admin-only.

Real: the engine, the lock files and real folders. Mocked: ``FakeSystem`` (process liveness, elevation, untrusted folders).
Out of scope: the command-line wording.
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
from gupkg.selfmgmt.elevation import UntrustedLocationError
from tests.support.engine import World

pytestmark = pytest.mark.unit

# Table that declares an environment variable pointing at ``App``.
ENV_ONLY = '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'
# Process id of a live process that holds a lock.
OTHER_PID = 424242


@pytest.fixture
def world_with_tool_update(tmp_path: Path) -> World:
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


def test_a_second_install_is_refused_naming_the_holder_and_the_holders_lock_stays(world_with_tool_update: World) -> None:
    """Exit status 3, the message names the process, the operation and the host; nothing changes and the lock is not removed."""
    package = world_with_tool_update.user / "tool"
    lock = hold(world_with_tool_update, package)
    before = (package / "current").resolve()

    result = world_with_tool_update.one("update", "tool")

    assert not result.ok and result.exit_code == 3 and result.error["code"] == "package-busy"
    assert str(OTHER_PID) in result.error["message"] and "install" in result.error["message"] and result.error.get("hint")
    assert (package / "current").resolve() == before and "othertoken" in lock.read_text(encoding="utf-8")


@pytest.mark.parametrize("operation", ["install", "uninstall", "cleanup", "check-updates"])
def test_every_operation_that_changes_a_package_takes_the_lock(world_with_tool_update: World, operation: str) -> None:
    """Install, uninstall, cleanup and check-updates see a living holder and fail with `package-busy` (exit 3), naming it."""
    hold(world_with_tool_update, world_with_tool_update.user / "tool")

    result = world_with_tool_update.one(operation, "tool")

    assert not result.ok and result.exit_code == 3 and result.error["code"] == "package-busy" and str(OTHER_PID) in result.error["message"]


def test_a_cleanup_sweep_leaves_a_busy_package_alone_without_failing(world_with_tool_update: World) -> None:
    """A pass over a whole root is not a request about one package: the busy one is `locked`, the call succeeds, the summary says why."""
    world_with_tool_update.publish("free", "1.0.0")
    assert world_with_tool_update.one("install", "free").ok
    hold(world_with_tool_update, world_with_tool_update.user / "tool")

    batch = world_with_tool_update.run("cleanup", "tool", "free", sweep=True)

    assert [(item.package, item.status) for item in batch.results] == [("tool", "locked"), ("free", "cleaned")]
    assert batch.ok and batch.exit_code == 0 and batch.results[0].details["locked"] is True
    assert "Left alone (in use by another run): tool" in batch.summary() and "after cancel" not in batch.summary()


def test_check_reads_only_but_check_fix_takes_the_lock(world_with_tool_update: World) -> None:
    """A plain check works while another run holds the package; --fix is refused."""
    hold(world_with_tool_update, world_with_tool_update.user / "tool")

    assert world_with_tool_update.one("check", "tool").ok
    refused = world_with_tool_update.one("check", "tool", fix=True)

    assert not refused.ok and refused.error["code"] == "package-busy"


def test_a_lock_of_a_dead_process_is_reclaimed_and_released_after_the_run(world_with_tool_update: World) -> None:
    """The stale lock is replaced, the run succeeds and no lock file or folder is left."""
    package = world_with_tool_update.user / "tool"
    hold(world_with_tool_update, package, live=False)

    result = world_with_tool_update.one("update", "tool")

    assert result.ok and not lock_path(package).exists() and not lock_path(package).parent.exists()


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        pytest.param({"age_days": 2}, None, id="older-than-a-day-though-the-process-lives"),
        pytest.param({"age_days": 2, "host": "elsewhere"}, None, id="old-lock-of-another-host"),
        pytest.param({"age_days": 1, "live": False}, "garbage = [", id="unreadable-and-old"),
    ],
)
def test_an_old_or_unreadable_old_lock_is_reclaimed(world_with_tool_update: World, kwargs: dict, text: str | None) -> None:
    """A recycled process id cannot hold a lock for ever; an old unreadable file is no lock."""
    package = world_with_tool_update.user / "tool"
    hold(world_with_tool_update, package, text=text, **kwargs)

    assert world_with_tool_update.one("update", "tool").ok


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        pytest.param({"host": "elsewhere", "live": False}, None, id="another-host-judged-by-age-only"),
        pytest.param({"live": False}, "garbage = [", id="unreadable-but-brand-new"),
    ],
)
def test_a_lock_that_cannot_be_judged_dead_is_respected(world_with_tool_update: World, kwargs: dict, text: str | None) -> None:
    """A lock of another host or one still being written is not stolen."""
    hold(world_with_tool_update, world_with_tool_update.user / "tool", text=text, **kwargs)

    result = world_with_tool_update.one("update", "tool")

    assert not result.ok and result.error["code"] == "package-busy"


def test_cleanup_never_removes_a_work_folder_younger_than_a_day_even_when_unlocked(world_with_tool_update: World) -> None:
    """The in-flight `populate-...` staging folder survives a cleanup; an old one goes."""
    package = world_with_tool_update.user / "tool"
    young = package / ".gupkg" / "work" / "populate-young"
    young.mkdir(parents=True)
    (young / "half.bin").write_text("x", encoding="utf-8")
    old = package / ".gupkg" / "work" / "populate-old"
    old.mkdir(parents=True)
    ancient = time.time() - 3 * 24 * 3600
    os.utime(old, (ancient, ancient))

    result = world_with_tool_update.one("cleanup", "tool")

    assert result.ok and result.details["work"] == ["populate-old"] and young.is_dir() and not old.exists()


def test_cleanup_of_a_locked_package_removes_nothing(world_with_tool_update: World) -> None:
    """A package another run is changing is left whole, even its old receipts and work entries, whether named or swept."""
    package = world_with_tool_update.user / "tool"
    work = package / ".gupkg" / "work" / "populate-inflight"
    work.mkdir(parents=True)
    ancient = time.time() - 3 * 24 * 3600
    os.utime(work, (ancient, ancient))
    hold(world_with_tool_update, package)

    named = world_with_tool_update.one("cleanup", "tool")
    swept = world_with_tool_update.one("cleanup", "tool", sweep=True)

    assert named.error["code"] == "package-busy" and swept.status == "locked" and work.is_dir()


def test_the_lock_is_released_after_a_failure_an_exception_and_a_cancel(world_with_tool_update: World, tmp_path: Path) -> None:
    """No path leaves the lock behind: a refused download, an exception inside the block, and a cancelled batch."""
    # Arrange: an installed package with a tracker.
    package = world_with_tool_update.user / "tool"
    # A refused install leaves no lock.
    world_with_tool_update.publish("other", "1.0.0", hashed=False)
    refused = world_with_tool_update.one("install", "other")
    assert not refused.ok and not lock_path(world_with_tool_update.user / "other").exists()

    # An exception inside the lock releases it.
    with pytest.raises(RuntimeError):
        with package_lock(world_with_tool_update.fake, package, "install", world_with_tool_update.engine.now, scope=world_with_tool_update.engine.scope):
            assert lock_path(package).is_file()
            raise RuntimeError("boom")
    assert not lock_path(package).exists()

    # A cancel between two packages releases every lock.
    world_with_tool_update.publish("one", "1.0.0")
    world_with_tool_update.publish("two", "1.0.0")
    marker = tmp_path / "stop"
    world_with_tool_update.engine.cancel = stop_marker(marker)
    calls = []
    original = world_with_tool_update.engine.cancel

    # The cancel is requested as the second package begins.
    def cancel_after_first() -> bool:
        """Request the cancel while the second package begins."""
        calls.append(1)
        if len(calls) == 2:
            marker.write_text("", encoding="utf-8")
        return original()

    # Run the batch: the second package is skipped and no lock is left.
    world_with_tool_update.engine.cancel = cancel_after_first
    batch = world_with_tool_update.run("install", "one", "two")
    assert [item.status for item in batch.results][1] == "skipped"
    assert not lock_path(world_with_tool_update.user / "one").exists() and not lock_path(world_with_tool_update.user / "two").exists()


def test_a_package_folder_made_for_the_lock_is_removed_when_nothing_came_to_live_in_it(world_with_tool_update: World) -> None:
    """The lock creates the folder of a package that does not exist yet; with nothing written in it, it is gone again."""
    fresh = world_with_tool_update.user / "fresh"

    with pytest.raises(RuntimeError):
        with package_lock(world_with_tool_update.fake, fresh, "install", world_with_tool_update.engine.now, scope=world_with_tool_update.engine.scope):
            assert lock_path(fresh).is_file()
            raise RuntimeError("the install failed before it wrote anything")
    assert not fresh.exists()

    with package_lock(world_with_tool_update.fake, fresh, "install", world_with_tool_update.engine.now, scope=world_with_tool_update.engine.scope):
        (fresh / "1.0.0").mkdir()
    assert (fresh / "1.0.0").is_dir() and not lock_path(fresh).parent.exists()


def test_a_refused_first_install_from_a_distribution_leaves_no_lock_behind(world_with_tool_update: World) -> None:
    """A distribution install that the checksum rules refuse releases its lock, in a package that did not exist."""
    world_with_tool_update.publish("fresh", "1.0.0", hashed=False, root=world_with_tool_update.base / "downloads")

    result = world_with_tool_update.one("install", str(world_with_tool_update.base / "downloads" / "fresh" / "1.0.0"))

    assert not result.ok and not lock_path(world_with_tool_update.user / "fresh").exists()

def test_the_lock_holder_is_visible_without_taking_the_lock(world_with_tool_update: World) -> None:
    """lock_holder reports a live holder and ignores a stale one; it never changes a file."""
    package = world_with_tool_update.user / "tool"
    assert lock_holder(world_with_tool_update.fake, package, world_with_tool_update.engine.now()) is None
    path = hold(world_with_tool_update, package)
    before = path.read_bytes()

    holder = lock_holder(world_with_tool_update.fake, package, world_with_tool_update.engine.now())

    assert holder is not None and holder.pid == OTHER_PID and path.read_bytes() == before
    world_with_tool_update.fake.live_pids.clear()
    assert lock_holder(world_with_tool_update.fake, package, world_with_tool_update.engine.now()) is None


def test_package_lock_refuses_a_live_holder_directly(world_with_tool_update: World) -> None:
    """The context manager itself raises PackageBusy with the holder named."""
    package = world_with_tool_update.user / "tool"
    hold(world_with_tool_update, package)

    with pytest.raises(PackageBusy, match=str(OTHER_PID)):
        with package_lock(world_with_tool_update.fake, package, "install", world_with_tool_update.engine.now, scope=world_with_tool_update.engine.scope):
            pytest.fail("the lock was taken")


def test_an_elevated_run_refuses_a_locks_folder_that_is_not_admin_only(elevated_world: World) -> None:
    """A standard user's writable locks folder cannot hold the lock of an administrator's run."""
    elevated_world.publish("tool", "1.0.0", mode="system", tables=ENV_ONLY)
    elevated_world.fake.untrusted_dirs = {Path(os.path.abspath(elevated_world.system_root / "tool" / ".gupkg" / "locks"))}

    with pytest.raises(UntrustedLocationError, match="admin-only"):
        with package_lock(elevated_world.fake, elevated_world.system_root / "tool", "install", elevated_world.engine.now, scope=elevated_world.engine.scope):
            pytest.fail("the lock was taken")
    result = elevated_world.one("install", "tool")

    assert not result.ok and result.error["code"] == "untrusted-location"


def test_an_elevated_install_locks_inside_the_system_package_and_never_writes_the_users_root(elevated_world: World) -> None:
    """While the administrator's install runs its lock file is below the system package; nothing is written in the user's root."""
    elevated_world.publish("tool", "1.0.0", mode="system", tables=ENV_ONLY)
    seen: list[Path] = []

    # The executor asks for the time while the lock is held, so the lock files can be seen there.
    def clock():
        """Record the lock files that exist when the executor starts, then return the time."""
        seen.extend(elevated_world.base.rglob("package.lock"))
        return datetime.now(timezone.utc)

    elevated_world.engine.clock = clock

    # Act: install the package as an administrator.
    result = elevated_world.one("install", "tool")

    # Assert: the lock lived below the system package, and the user's root was not touched.
    assert result.ok and seen and {item.parent.parent.parent for item in seen} == {elevated_world.system_root / "tool"}
    assert not any("tool" in item.name for item in elevated_world.user.rglob("*"))
    assert not lock_path(elevated_world.system_root / "tool").exists()


@pytest.mark.parametrize("repeat", range(8))
def test_racing_runs_never_hold_the_lock_together(world_with_tool_update: World, repeat: int) -> None:
    """Sixteen threads ask for one package's lock at once: at no moment do two hold it, and it is gone at the end.

    Repeated, because the first version of the release gave up when a racing reader held the file open (Windows refuses
    to delete it then) and left a lock that a living process held for ever.
    """
    # Arrange: the counters for the race.
    package = world_with_tool_update.user / "tool"
    inside = [0]
    most = [0]
    guard = threading.Lock()
    refused = [0]

    def run() -> None:
        """Take the lock briefly, or count the refusal."""
        try:
            # Hold the lock briefly while counting how many runs hold it at once.
            with package_lock(world_with_tool_update.fake, package, "install", world_with_tool_update.engine.now, scope=world_with_tool_update.engine.scope):
                with guard:
                    inside[0] += 1
                    most[0] = max(most[0], inside[0])
                time.sleep(0.02)
                with guard:
                    inside[0] -= 1
        # A busy package is refused.
        except PackageBusy:
            with guard:
                refused[0] += 1

    # Start sixteen runs that race for the lock.
    threads = [threading.Thread(target=run) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Assert: never two holders at once, some runs were refused and the lock is gone.
    assert most[0] == 1 and refused[0] >= 1 and not lock_path(package).exists()
