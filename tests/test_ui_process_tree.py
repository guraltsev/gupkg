"""Cover what happens to the real command when a call is abandoned, hangs or finishes without exiting: nothing is ever left running unseen.

Behaviour covered: the launcher is a ``.cmd`` file, so the process the UI holds is ``cmd.exe`` and the Python command is its child; when a call is cancelled (the program closes), times out or is
killed after a cancel request, the whole tree is ended (the child is gone, not left running headless); an abandoned call is first asked to stop after its current package and given a bounded time to do so;
a command that wrote ``run_end`` but does not exit is stopped after a grace period and its result is still used, with a warning; a call that is quiet for a while is reported as still running without being
killed; a result without its ``run_end`` is not believed when the UI asked for an events file and the call claims success (a failure document is kept, with a warning), while plain read-only calls stay lenient.

Real: the client, real ``cmd.exe`` and Python processes (a ``.cmd`` stub that starts a Python grandchild), real files, real ``taskkill``. Mocked: the command's behaviour (a stub).
Out of scope: the screens (``test_ui_screens.py``) and the real CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import pytest

from gupkg_ui import client as client_module
from gupkg_ui.client import ClientError, StopRequest
from tests.ui_support import FakeGupkg, GrandchildStub, entry, process_alive, result

pytestmark = pytest.mark.windows_integration


@pytest.fixture
def stub(tmp_path: Path) -> GrandchildStub:
    """A ``.cmd`` launcher with a Python grandchild."""
    return GrandchildStub(tmp_path)


async def child_started(stub: GrandchildStub, timeout: float = 30.0) -> int:
    """Wait (bounded) until the grandchild has written its process id and its run_start, and return the id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = stub.child_pid()
        if pid is not None and (not stub.logs.exists() or any(stub.logs.iterdir())):
            return pid
        await asyncio.sleep(0.05)
    raise AssertionError("the stub did not start")


def gone(pid: int, timeout: float = 15.0) -> bool:
    """Wait (bounded) until process *pid* no longer exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_cancelling_a_call_ends_the_whole_process_tree(stub: GrandchildStub) -> None:
    """The program closing (the task is cancelled) leaves no Python child running: it is killed with its ``cmd.exe`` parent."""
    stub.set_mode("hang")

    async def scenario() -> int:
        """Start the call, wait for the grandchild, cancel the call."""
        task = asyncio.ensure_future(stub.client().call(("update", "--", "user:a"), events_dir=None))
        pid = await child_started(stub)
        assert process_alive(pid)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return pid

    assert gone(asyncio.run(scenario()))


def test_an_abandoned_call_is_first_asked_to_stop_and_given_time(stub: GrandchildStub) -> None:
    """With a stop marker announced the command is asked to stop after its current package, writes its ``run_end`` and exits on its own before the tree is ended."""
    stub.set_mode("stop-aware")
    stop = StopRequest(stub.temp)

    async def scenario() -> int:
        """Start, wait for run_start, cancel."""
        task = asyncio.ensure_future(stub.client().call(("update", "--", "user:a"), events_dir=stub.logs, stop=stop))
        pid = await child_started(stub)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return pid

    started = time.monotonic()
    pid = asyncio.run(scenario())

    assert (stub.dir / "saw_stop.txt").read_text(encoding="utf-8") == "True" and gone(pid)
    assert time.monotonic() - started < client_module.STOP_GRACE_SECONDS + 10


def test_a_timeout_ends_the_whole_tree(stub: GrandchildStub) -> None:
    """A read-only call that does not answer in time fails with a clear message and leaves nothing running."""
    stub.set_mode("hang")

    async def scenario() -> int:
        """Query with a short timeout."""
        with pytest.raises(ClientError, match="did not finish within"):
            await stub.client().query(("list",), timeout=1.0)
        return stub.child_pid()

    assert gone(asyncio.run(scenario()))


def test_a_call_that_ignores_the_cancel_request_is_killed_after_the_force_time(stub: GrandchildStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a cancel request the call has ``FORCE_STOP_SECONDS``; then its tree is killed and the call is a failure that says so."""
    monkeypatch.setattr(client_module, "FORCE_STOP_SECONDS", 0.5)
    stub.set_mode("ignore-stop")
    stop = StopRequest(stub.temp)

    async def scenario():
        """Start, request the stop, wait for the result."""
        task = asyncio.ensure_future(stub.client().call(("update", "--", "user:a"), events_dir=stub.logs, stop=stop))
        await child_started(stub)
        stop.request()
        return await task

    outcome = asyncio.run(scenario())

    assert not outcome.ok and any("stopped by force" in problem for problem in outcome.problems)
    assert gone(stub.child_pid())


def test_a_command_that_hangs_after_run_end_is_stopped_and_its_result_is_used(stub: GrandchildStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_end`` was written, the process never exits: after the grace time the tree is killed, the result of ``run_end`` stands, and a warning says what happened."""
    monkeypatch.setattr(client_module, "EXIT_GRACE_SECONDS", 0.5)
    stub.set_mode("finish-then-hang")

    outcome = asyncio.run(stub.client().call(("update", "--", "user:a"), events_dir=stub.logs))

    assert outcome.ok and outcome.problems == () and any("did not exit after finishing" in note for note in outcome.warnings)
    assert gone(stub.child_pid())


def test_a_quiet_call_is_reported_as_still_running_and_not_killed(stub: GrandchildStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """After ``QUIET_SECONDS`` without output the owner is told, and the call still finishes normally."""
    monkeypatch.setattr(client_module, "QUIET_SECONDS", 0.3)
    stub.set_mode("slow")
    told: list[float] = []

    outcome = asyncio.run(stub.client().call(("update", "--", "user:a"), events_dir=stub.logs, on_idle=told.append))

    assert outcome.ok and told and told[0] >= 0.3


def test_a_success_without_run_end_is_not_believed_but_a_failure_document_is_kept(tmp_path: Path) -> None:
    """The UI always asks for an events file: a document that claims success with no file at all is a failure; a failure document stays readable (with a warning); read-only calls stay lenient."""
    fake = FakeGupkg(tmp_path)
    fake.respond("update", doc=result("update", [entry("p")]))
    claimed = asyncio.run(fake.client().call(("update", "--", "user:p"), events_dir=tmp_path / "logs"))
    failing = result("check", [entry("p", "check", ok=False, status="failed", code=3, error={"code": "x", "message": "broken"})])
    fake.respond("check", doc=failing)
    failed = asyncio.run(fake.client().call(("check", "--", "user:p"), events_dir=tmp_path / "logs"))
    fake.respond("registry", doc={"api": 2, "command": "registry list", "ok": True, "exit_code": 0, "registries": []})
    listing = asyncio.run(fake.client().query(("registry", "list")))

    assert not claimed.ok and "no events file was written" in claimed.problems[0] and "exit code 0" in claimed.problems[0]
    assert failed.problems == () and failed.warnings and [item["package"] for item in failed.entries()] == ["p"] and not failed.ok
    assert listing["registries"] == []
