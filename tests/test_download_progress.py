"""Cover download size and progress feedback visible to command-line and UI users.

Behaviour covered: a download of known length announces its total and ends with one event whose ``done`` equals its total (not
two); redirected human output of an install has exactly one final progress line per download and no carriage returns, while
``--json`` keeps standard output a single document; a server that omits the size is shown as bytes received, and the final event
states the size that was learned; a UI receives transient progress text and one committed 100 percent line.

Real: the HTTP client policy (``gupkg.services.http``), the engine, the argument parser, the event stream and the plain-text
renderer. Mocked: a ``FakeSystem`` that serves the downloaded bytes (and announces, or withholds, their size). Real network
behaviour and the Rich terminal bar (``test_console_renderer.py``) are out of scope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gupkg.report.console import LineReporter
from gupkg.report.events import MultiReporter, Progress, use_reporter
from gupkg.services.http import HttpClient
from gupkg.system.fake import FakeResponse, FakeSystem
from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401


class _Collector:
    """Collect every event handed to it, in order."""

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event) -> None:
        self.events.append(event)


def download(system: FakeSystem, tmp_path: Path, content: bytes, *reporters) -> list[Progress]:
    """Download a fixture through the HTTP client with *reporters* installed; return the progress events it sent."""
    collector = _Collector()
    with use_reporter(MultiReporter([collector, *reporters])):
        HttpClient(system, cache_dir=tmp_path / "cache", env={}, retries=0).download("https://example.invalid/payload", label="Downloading update")
    return [event for event in collector.events if isinstance(event, Progress)]


def test_a_download_of_known_size_announces_its_total_and_completes_once(tmp_path: Path) -> None:
    """The first event carries the total, and exactly one event ends the transfer with done equal to total."""
    system = FakeSystem(tmp_path / "machine")
    content = b"x" * (300 * 1024)
    system.add_url("https://example.invalid/payload", content)

    progress = download(system, tmp_path, content)

    assert progress[0] == Progress("Downloading update", 0, len(content))
    assert [event for event in progress if event.done == event.total] == [Progress("Downloading update", len(content), len(content))]


def test_a_redirected_install_prints_one_final_progress_line_and_json_keeps_standard_output_clean(tmp_path: Path) -> None:
    """Human output has one 100 percent line per download and no carriage return; --json standard output is only the document."""
    world = CliWorld(tmp_path)
    world.publish("tool", "1.0.0", members={"tool.exe": "payload" * 5000})
    world.publish("other", "1.0.0", members={"other.exe": "payload" * 5000})

    human = world.cli("install", "tool")
    machine = world.cli("install", "other", "--json")

    lines = [line for line in human.out.splitlines() if "(100%)" in line]
    assert human.code == 0 and len(lines) == 1 and "tool-1.0.0.zip" in lines[0] and "\r" not in human.out
    assert json.loads(machine.out)["ok"] is True
    assert "==>" not in machine.out and len([line for line in machine.err.splitlines() if "(100%)" in line]) == 1


def test_the_events_file_of_an_install_carries_the_progress_of_the_download(tmp_path: Path) -> None:
    """A UI that follows --events-file sees the total up front and a last event that equals it."""
    world = CliWorld(tmp_path)
    world.publish("tool", "1.0.0", members={"tool.exe": "payload" * 5000})

    assert world.cli("install", "tool", "--events-file", "run.jsonl").code == 0

    records = [json.loads(line) for line in (world.user / "tool" / ".gupkg" / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    progress = [record for record in records if record["event"] == "progress"]
    assert progress[0]["done"] == 0 and progress[0]["total"] and progress[-1]["done"] == progress[-1]["total"] == progress[0]["total"]
    assert sum(record["done"] == record["total"] for record in progress) == 1


def test_a_download_of_unknown_size_reports_received_bytes(tmp_path: Path) -> None:
    """A server that omits the size is shown as bytes received, and the final event states the size it learned."""
    system = FakeSystem(tmp_path / "machine")
    content = b"x" * (200 * 1024)
    system.add_url("https://example.invalid/payload", FakeResponse(content, report_size=False))
    lines: list[str] = []
    transients: list[str] = []

    progress = download(system, tmp_path, content, LineReporter(lines.append, transients.append))

    assert progress[0] == Progress("Downloading update", 0, None)
    assert all(event.total is None for event in progress[:-1])
    assert progress[-1] == Progress("Downloading update", len(content), len(content))
    assert [line.strip() for line in lines] == ["Downloading update: 200.0 KiB / 200.0 KiB (100%)"]


def test_a_live_ui_gets_transient_progress_then_one_final_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Intermediate progress is transient text, and the committed lines are exactly one 100 percent line."""
    system = FakeSystem(tmp_path / "machine")
    content = b"x" * (256 * 1024)
    system.add_url("https://example.invalid/payload", content)
    clock = iter(float(index) for index in range(10_000))
    monkeypatch.setattr("gupkg.services.http.time.monotonic", lambda: next(clock))
    lines: list[str] = []
    transients: list[str] = []

    download(system, tmp_path, content, LineReporter(lines.append, transients.append))

    assert transients and all("(100%)" not in text for text in transients)
    assert len(lines) == 1 and "(100%)" in lines[0]
