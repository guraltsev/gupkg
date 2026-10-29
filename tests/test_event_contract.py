"""Pin the event stream's public JSON contract and the rules of how events are delivered.

The event classes, the active-reporter channel and the JSON Lines written by a real
command-line run are real. The machine boundary is a ``FakeSystem``. Rendering
(``test_console_renderer.py``), log file placement (``test_run_logs.py``) and the result documents
(``test_cli_machine_interface.py``) are out of scope.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tests.cli_support import CliWorld
from gupkg.report import events
from gupkg.report.events import (
    EVENT_TYPES, Diagnostic, Error, MultiReporter, PackageEnd, PackageStart, PhaseEnd, PhaseStart,
    Progress, Prompt, RunEnd, RunStart, Step, SubprocessLine, Warning, current_reporter,
    emit, emit_problems, event_to_dict, package_scope, phase, subprocess_lines, use_reporter,
)


class _Collector:
    """Collect every event handed to it, in order."""

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event) -> None:
        self.events.append(event)


# One example of every event type and the exact JSON record it must produce.
# A field or event name changing here is a break of the machine contract
# (``"api": 2``): UI programs and scripts read these records.
CONTRACT = [
    pytest.param(
        RunStart("update", ("update", "firefox"), "user", False, "1.0.0", ("user",)),
        {"event": "run_start", "command": "update", "argv": ["update", "firefox"], "mode": "user",
         "elevated": False, "version": "1.0.0", "settings_layers": ["user"], "api": 2, "stop_file": None},
        id="run_start",
    ),
    pytest.param(
        RunStart("update", ("update", "firefox"), "user", False, "1.0.0", (), stop_file="C:\\Temp\\gupkg-stop-1"),
        {"event": "run_start", "command": "update", "argv": ["update", "firefox"], "mode": "user",
         "elevated": False, "version": "1.0.0", "settings_layers": [], "api": 2, "stop_file": "C:\\Temp\\gupkg-stop-1"},
        id="run_start-with-the-stop-marker",
    ),
    pytest.param(
        PackageStart({"name": "firefox", "path": "C:\\opt\\firefox"}),
        {"event": "package_start", "view": {"name": "firefox", "path": "C:\\opt\\firefox"}},
        id="package_start",
    ),
    pytest.param(PhaseStart("Fetch"), {"event": "phase_start", "phase": "Fetch"}, id="phase_start"),
    pytest.param(
        Step("shortcut Firefox", "add"),
        {"event": "step", "text": "shortcut Firefox", "kind": "add"},
        id="step",
    ),
    pytest.param(
        Progress("setup.exe", 50, 100),
        {"event": "progress", "task": "setup.exe", "done": 50, "total": 100, "unit": "B"},
        id="progress",
    ),
    pytest.param(
        Progress("setup.exe", 50, None, "files"),
        {"event": "progress", "task": "setup.exe", "done": 50, "total": None, "unit": "files"},
        id="progress-unknown-total",
    ),
    pytest.param(
        SubprocessLine("git", "fatal: not a repository"),
        {"event": "subprocess_line", "cmd": "git", "line": "fatal: not a repository", "stream": "output"},
        id="subprocess_line",
    ),
    pytest.param(
        Prompt("Continue?", "n"),
        {"event": "prompt", "text": "Continue?", "default": "n"},
        id="prompt",
    ),
    pytest.param(Warning("careful"), {"event": "warning", "text": "careful"}, id="warning"),
    pytest.param(Diagnostic("Traceback"), {"event": "diagnostic", "text": "Traceback"}, id="diagnostic"),
    pytest.param(
        Error("it broke", "run gupkg check --fix"),
        {"event": "error", "text": "it broke", "hint": "run gupkg check --fix"},
        id="error",
    ),
    pytest.param(
        PhaseEnd("Fetch", "ok", 1.5),
        {"event": "phase_end", "phase": "Fetch", "status": "ok", "elapsed": 1.5},
        id="phase_end",
    ),
    pytest.param(
        PackageEnd({"name": "firefox", "ok": True, "status": "changed"}),
        {"event": "package_end", "result": {"name": "firefox", "ok": True, "status": "changed"}},
        id="package_end",
    ),
    pytest.param(
        RunEnd("install: changed", 0),
        {"event": "run_end", "summary": "install: changed", "exit_code": 0, "result": None},
        id="run_end",
    ),
    pytest.param(
        RunEnd("install: changed", 0, {"api": 2, "command": "install", "ok": True}),
        {"event": "run_end", "summary": "install: changed", "exit_code": 0,
         "result": {"api": 2, "command": "install", "ok": True}},
        id="run_end-with-the-result-document",
    ),
]


@pytest.mark.parametrize(("event", "record"), CONTRACT)
def test_every_event_type_has_a_pinned_json_shape(event, record) -> None:
    """Each event serialises to exactly its documented JSON record, and that record survives a JSON round trip."""
    assert event_to_dict(event) == record
    assert json.loads(json.dumps(event_to_dict(event))) == record


def test_a_diagnostic_traceback_is_redacted_like_every_other_text() -> None:
    """A traceback that holds a URL with credentials reaches the reporters (and so the log files) without them."""
    collector = _Collector()
    text = "Traceback (most recent call last):\nHttpError: https://user:hunter2@example.org/a.zip?token=SECRET123&x=1 failed"

    with use_reporter(collector):
        emit(Diagnostic(text))

    [event] = collector.events
    assert "hunter2" not in event.text and "SECRET123" not in event.text and "Traceback" in event.text and "example.org" in event.text


def test_the_contract_covers_every_event_type() -> None:
    """No event type exists without a pinned shape, so adding one forces a contract decision."""
    pinned = {type(param.values[0]) for param in CONTRACT}
    assert pinned == set(EVENT_TYPES)


def test_a_problem_is_delivered_once_per_package_and_once_in_the_run_summary() -> None:
    """A warning repeated live and in a result appears once, while two packages may each report the same words."""
    collector = _Collector()

    with use_reporter(collector):
        with package_scope("a", "C:\\opt\\a"):
            emit(Warning("shared words"))
            emit_problems(["shared words"], ["boom"])
        with package_scope("b", "C:\\opt\\b"):
            emit(Warning("shared words"))
        emit_problems(["shared words"], ["boom"])

    problems = [(type(e).__name__, e.text) for e in collector.events if isinstance(e, (Warning, Error))]
    assert problems == [("Warning", "shared words"), ("Error", "boom"), ("Warning", "shared words")]


def test_phase_reports_start_end_status_and_elapsed_time() -> None:
    """A phase ends ok by default, failed when its body raises or is marked, and skipped when it says so."""
    collector = _Collector()

    with use_reporter(collector):
        with phase("One"):
            pass
        with phase("Two") as two:
            two.fail()
        with phase("Three") as three:
            three.skip()
        with pytest.raises(RuntimeError):
            with phase("Four"):
                raise RuntimeError("stop")

    ends = [e for e in collector.events if isinstance(e, PhaseEnd)]
    assert [(e.phase, e.status) for e in ends] == [
        ("One", "ok"), ("Two", "failed"), ("Three", "skipped"), ("Four", "failed")
    ]
    assert all(e.elapsed >= 0 for e in ends)


def test_a_failing_reporter_is_dropped_with_one_warning_and_the_others_continue() -> None:
    """A reporter that raises stops reporting, the rest still receive every event, and they are told once."""
    class Broken:
        def handle(self, event) -> None:
            raise OSError("disk full")

    good = _Collector()
    hub = MultiReporter([Broken(), good])

    hub.handle(Step("one"))
    hub.handle(Step("two"))

    assert good.events == [Step("one"), Warning("Broken stopped reporting: disk full"), Step("two")]


def test_child_process_lines_reach_the_reporter_from_other_threads() -> None:
    """Lines reported by a reader thread (which has no context) still reach the reporter active when the callback was made."""
    collector = _Collector()

    with use_reporter(collector):
        on_line = subprocess_lines(["C:\\tools\\git.exe", "clone"])
    worker = threading.Thread(target=on_line, args=("remote: \x1b[31mfatal\x1b[0m\r",))
    worker.start()
    worker.join()

    assert collector.events == [SubprocessLine("git", "remote: fatal")]
    assert current_reporter() is None


def test_events_are_silently_dropped_without_a_reporter() -> None:
    """With no reporter installed emitting does nothing, so library code never prints or fails."""
    emit(Step("nobody listens"))
    emit(Warning("nobody listens"))
    assert events.current_reporter() is None


def test_a_real_cli_run_writes_only_contract_records_to_its_event_file(tmp_path: Path) -> None:
    """A real install through the CLI leaves JSON Lines in which every record is a pinned event (or the log link) with a timestamp."""
    world = CliWorld(tmp_path)
    world.publish("example", "1.0.0")

    result = world.cli("install", "example", "--events-file", "events.jsonl")

    assert result.code == 0, result.err
    run_events = (world.user / ".gupkg" / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    package_events = (world.user / "example" / ".gupkg" / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    wire_names = {event_to_dict(param.values[0])["event"] for param in CONTRACT} | {"log_link"}
    for line in run_events + package_events:
        record = json.loads(line)
        assert record["event"] in wire_names
        assert record["ts"].endswith("+00:00")
    assert json.loads(run_events[0])["event"] == "run_start" and json.loads(run_events[0])["api"] == 2
    assert json.loads(run_events[-1])["event"] == "run_end" and json.loads(run_events[-1])["result"]["api"] == 2
    [link] = [json.loads(line) for line in run_events if json.loads(line)["event"] == "log_link"]
    assert set(link) == {"event", "package", "log", "events", "events_file", "ts"}
    assert link["package"] == "example"
