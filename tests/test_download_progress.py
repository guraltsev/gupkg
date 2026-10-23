"""Cover download size and progress feedback visible to CLI users.

The ZIP extraction, the update workflow and the CLI rendering are real. The
machine boundary is a ``FakeSystem`` that serves the downloaded bytes (and
announces, or withholds, their size); archive contents and filesystem writes
remain real. Real network behavior is out of scope.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from gupkg import cli, downloads, origin
from gupkg.commands import LineStream
from gupkg.core import PackageIdentity
from gupkg.downloads import download_file
from gupkg.system.fake import FakeResponse, FakeSystem


def _archive() -> bytes:
    """Return a small valid application archive for origin population."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("tool.exe", "payload" * 5000)
    return archive.getvalue()


def test_zip_origin_reports_total_size_and_completion(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """A known-length origin download shows its total and reaches 100 percent."""
    version_path = tmp_path / "Example" / "1.0.0"
    version_path.mkdir(parents=True)
    identity = PackageIdentity.from_version_path(
        version_path.parent, version_path, is_current=False
    )
    archive = _archive()
    system.add_url("https://example.invalid/app.zip", archive)

    origin.populate_app_from_zip_origin(
        identity,
        {"url": "https://example.invalid/app.zip"},
        system,
        no_checksum=False,
    )

    output = capsys.readouterr().out
    assert "Downloading origin: total size " in output and "unknown" not in output
    assert "Downloading origin:" in output and "(100%)" in output
    assert (version_path / "App" / "tool.exe").is_file()


def test_human_cli_streams_progress_while_toml_keeps_stdout_parseable(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human output exposes download progress while TOML output contains only its document."""
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
    assert "Downloading update: total size" in human_output
    assert "Downloading update:" in human_output and "(100%)" in human_output

    # A second run in TOML mode finds the staged version and prints only the document.
    assert cli.main(["--format", "toml", "update", "--download-only", str(version_path)], system=system) == 0
    toml_output = capsys.readouterr().out
    assert "Downloading update" not in toml_output
    assert "output_schema = 1" in toml_output


def test_redirected_download_prints_total_and_one_final_line(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """A download to a non-live stream prints its total size and one final 100 percent line.

    Output does not grow with the size of the download.
    """
    content = b"x" * (300 * 1024)
    system.add_url("https://example.invalid/payload", content)

    download_file(system, "https://example.invalid/payload", tmp_path / "payload", label="Downloading update")

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Downloading update: total size 300.0 KiB"
    assert len(lines) == 2
    assert "(100%)" in lines[1]
    assert (tmp_path / "payload").read_bytes() == content


def test_download_of_unknown_size_reports_received_bytes(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """A server that omits the size still gets a total-unknown notice and a final received-bytes line."""
    content = b"x" * (200 * 1024)
    system.add_url("https://example.invalid/payload", FakeResponse(content, report_size=False))

    download_file(system, "https://example.invalid/payload", tmp_path / "payload", label="Downloading update")

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Downloading update: total size unknown"
    assert lines[-1] == "Downloading update: 200.0 KiB received"
    assert (tmp_path / "payload").read_bytes() == content


def test_download_into_a_live_line_stream_reports_transient_progress_then_one_final_line(
    tmp_path: Path, system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live stream receives intermediate progress as transient text and one committed 100 percent line."""
    # Make every chunk count as "enough time passed" so progress is intermediate.
    monkeypatch.setattr(downloads, "_REPORT_INTERVAL", 0.0)
    lines: list[str] = []
    transients: list[str] = []
    content = b"x" * (256 * 1024)
    system.add_url("https://example.invalid/payload", content)

    with contextlib.redirect_stdout(LineStream(lines.append, on_transient=transients.append)):
        download_file(system, "https://example.invalid/payload", tmp_path / "payload", label="Downloading update")

    assert transients
    assert lines[0] == "Downloading update: total size 256.0 KiB"
    assert [line for line in lines if "(100%)" in line] == [lines[-1]]
