"""Cover download size and progress feedback visible to CLI and UI users.

The ZIP extraction, the update workflow, the event stream, the plain-text
renderer and the CLI are real. The machine boundary is a ``FakeSystem`` that
serves the downloaded bytes (and announces, or withholds, their size);
archive contents and filesystem writes remain real. Real network behavior and
the Rich terminal bar (see ``test_console_renderer.py``) are out of scope.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from gupkg import cli, downloads, origin
from gupkg.core import PackageIdentity
from gupkg.downloads import download_file
from gupkg.report.console import LineReporter
from gupkg.report.events import MultiReporter, Progress, use_reporter
from gupkg.system.fake import FakeResponse, FakeSystem


class _Collector:
    """Collect every event handed to it, in order."""

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event) -> None:
        self.events.append(event)


def _archive() -> bytes:
    """Return a small valid application archive for origin population."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("tool.exe", "payload" * 5000)
    return archive.getvalue()


def test_zip_origin_reports_total_size_and_completion(tmp_path: Path, system: FakeSystem) -> None:
    """A known-length origin download announces its total and ends with done equal to total."""
    version_path = tmp_path / "Example" / "1.0.0"
    version_path.mkdir(parents=True)
    identity = PackageIdentity.from_version_path(
        version_path.parent, version_path, is_current=False
    )
    archive = _archive()
    system.add_url("https://example.invalid/app.zip", archive)
    collector = _Collector()

    with use_reporter(collector):
        origin.populate_app_from_zip_origin(
            identity,
            {"url": "https://example.invalid/app.zip"},
            system,
            no_checksum=False,
        )

    progress = [event for event in collector.events if isinstance(event, Progress)]
    assert progress[0] == Progress("Downloading origin", 0, len(archive))
    assert progress[-1] == Progress("Downloading origin", len(archive), len(archive))
    assert (version_path / "App" / "tool.exe").is_file()


def test_human_cli_shows_one_final_progress_line_while_toml_keeps_stdout_parseable(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """Redirected human output has one final progress line per download; TOML stdout holds only its document."""
    version_path = tmp_path / "Example" / "1.0.0"
    (version_path / "App").mkdir(parents=True)
    (version_path / "pkg.toml").write_text(
        'name = "Example"\nversion = "1.0.0"\nlocalVersion = 0\n\n'
        '[origin]\nurl = "https://github.com/owner/example"\n\n'
        '[update.check]\nmode = "github"\nassetName = "example.zip"\n\n'
        '[update.payload]\nmode = "zip"\n',
        encoding="utf-8",
    )
    archive = _archive()
    system.add_url(
        "https://api.github.com/repos/owner/example/releases/latest",
        json.dumps({
            "id": 1,
            "tag_name": "2.0.0",
            "assets": [{
                "name": "example.zip",
                "state": "uploaded",
                "browser_download_url": "https://example.invalid/example.zip",
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            }],
        }),
    )
    system.add_url("https://example.invalid/example.zip", archive)

    assert cli.main(["update", "--download-only", str(version_path)], system=system) == 0
    human_output = capsys.readouterr().out
    progress_lines = [line for line in human_output.splitlines() if "Downloading update:" in line]
    assert len(progress_lines) == 1 and "(100%)" in progress_lines[0]
    assert "\r" not in human_output

    # A second run in TOML mode finds the staged version; its standard output
    # is only the document and the human text moves to standard error.
    assert cli.main(["--format", "toml", "update", "--download-only", str(version_path)], system=system) == 0
    captured = capsys.readouterr()
    assert "output_schema = 1" in captured.out
    assert "==>" not in captured.out and "Downloading update" not in captured.out
    assert "==>" in captured.err


def test_redirected_download_prints_one_final_line_whatever_its_size(
    tmp_path: Path, system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download rendered as plain text prints one final 100 percent line, however many progress events it sent."""
    # Make every chunk count as "enough time passed" so many intermediate events are sent.
    monkeypatch.setattr(downloads, "_REPORT_INTERVAL", 0.0)
    content = b"x" * (300 * 1024)
    system.add_url("https://example.invalid/payload", content)
    lines: list[str] = []

    with use_reporter(LineReporter(lines.append)):
        download_file(system, "https://example.invalid/payload", tmp_path / "payload", label="Downloading update")

    assert [line.strip() for line in lines] == ["Downloading update: 300.0 KiB / 300.0 KiB (100%)"]
    assert (tmp_path / "payload").read_bytes() == content


def test_download_of_unknown_size_reports_received_bytes(
    tmp_path: Path, system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that omits the size is shown as bytes received, and the final event states the size it learned."""
    monkeypatch.setattr(downloads, "_REPORT_INTERVAL", 0.0)
    content = b"x" * (200 * 1024)
    system.add_url("https://example.invalid/payload", FakeResponse(content, report_size=False))
    lines: list[str] = []
    transients: list[str] = []
    collector = _Collector()

    with use_reporter(MultiReporter([collector, LineReporter(lines.append, transients.append)])):
        download_file(system, "https://example.invalid/payload", tmp_path / "payload", label="Downloading update")

    progress = [event for event in collector.events if isinstance(event, Progress)]
    assert progress[0] == Progress("Downloading update", 0, None)
    assert all(event.total is None for event in progress[:-1])
    assert progress[-1] == Progress("Downloading update", len(content), len(content))
    assert transients and all(text.endswith("received") for text in transients)
    assert (tmp_path / "payload").read_bytes() == content


def test_download_into_a_live_ui_reports_transient_progress_then_one_final_line(
    tmp_path: Path, system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UI receives intermediate progress as transient text and one committed 100 percent line."""
    monkeypatch.setattr(downloads, "_REPORT_INTERVAL", 0.0)
    lines: list[str] = []
    transients: list[str] = []
    content = b"x" * (256 * 1024)
    system.add_url("https://example.invalid/payload", content)

    with use_reporter(LineReporter(lines.append, transients.append)):
        download_file(system, "https://example.invalid/payload", tmp_path / "payload", label="Downloading update")

    assert transients
    assert all("(100%)" not in text for text in transients)
    assert [line for line in lines if "(100%)" in line] == lines
    assert len(lines) == 1
