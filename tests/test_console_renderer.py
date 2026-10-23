"""Cover what a person sees on the console for a run's events: terminal, redirected output and NO_COLOR.

The renderer is real, driven with an event sequence like the one an install
produces. A terminal is simulated with a recording Rich ``Console`` that
claims to be a terminal; redirected output is an in-memory text stream; real
child-process output (carriage returns, colour codes, non-ASCII text) comes
from an actual Python child run through ``WindowsSystem``. Real Windows
console modes and the UI programs' own rendering are out of scope.
"""

from __future__ import annotations

import io
import re
import sys

import pytest

from gupkg.report.console import ConsoleReporter
from gupkg.report.events import (
    Error, PackageEnd, PackageStart, PhaseEnd, PhaseStart, Progress, RunEnd, RunStart, Step,
    SubprocessLine, Warning, subprocess_lines, use_reporter,
)

MIB = 1024 * 1024


def _install_events() -> list:
    """Return the events of a small install that downloads one file, warns once and fails verification."""
    events: list = [
        RunStart("update", ("update", "firefox"), "user", False, "1.0.0"),
        PackageStart({"name": "firefox", "path": "C:\\opt\\firefox"}),
        PhaseStart("Fetch"),
        Progress("setup.exe", 0, 74 * MIB),
    ]
    events += [Progress("setup.exe", done * MIB, 74 * MIB) for done in (10, 30, 50)]
    events += [
        Progress("setup.exe", 74 * MIB, 74 * MIB),
        Step("sha256 matches [vendor]", "ok"),
        PhaseEnd("Fetch", "ok", 1.25),
        PhaseStart("Verify"),
        Warning("checksum came from the vendor page"),
        SubprocessLine("git", "fatal: repository 'https://example.invalid/x.git' not found"),
        Error("verification failed", "check the URL"),
        PhaseEnd("Verify", "failed", 0.3),
        PackageEnd({"name": "firefox", "ok": False, "status": "failed"}),
        RunEnd("update: failed\nRun log: C:\\opt\\.gupkg\\logs\\run.log", 3),
    ]
    return events


def _feed(reporter: ConsoleReporter, events: list) -> None:
    for event in events:
        reporter.handle(event)


@pytest.fixture
def terminal():
    """A recording Rich console that behaves like a colour terminal, and where its raw output goes."""
    from rich.console import Console

    raw = io.StringIO()
    console = Console(record=True, force_terminal=True, width=100, color_system="truecolor", file=raw)
    return console, raw


def test_terminal_shows_coloured_phase_rules_marks_a_live_bar_and_ends_with_the_summary(terminal, monkeypatch) -> None:
    """On a terminal each phase has a bold coloured rule and a check or cross with its time, a live bar becomes one lasting line, and the summary is last."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    console, raw = terminal

    _feed(ConsoleReporter(console=console), _install_events())

    text = console.export_text()
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    assert any(line.startswith("Fetch ") and "━━━" in line for line in lines)
    assert "  ✓ Fetch  1.2s" in lines
    assert "  ✗ Verify failed  0.3s" in lines
    assert "WARNING: checksum came from the vendor page" in lines
    assert "ERROR: verification failed" in lines and "  hint: check the URL" in lines
    assert "    ✓ sha256 matches [vendor]" in lines
    assert "      │ fatal: repository 'https://example.invalid/x.git' not found" in lines

    # The live bar showed progress while the download ran, then left one lasting line.
    lasting = "✓ setup.exe: 74.0 MiB / 74.0 MiB (100%)"
    assert text.index("30.0/74.0 MiB") < text.index(lasting)
    assert text.count(lasting) == 1
    assert raw.getvalue().rindex("[2K") < raw.getvalue().index(lasting), "the bar is erased before the lasting line"

    # The summary is the last thing printed, and the raw stream carries colour.
    assert lines[-2:] == ["    update: failed", "    Run log: C:\\opt\\.gupkg\\logs\\run.log"]
    assert "\x1b[1;36m" in raw.getvalue()


def test_redirected_output_is_plain_text_with_phase_headers_and_one_progress_line(monkeypatch) -> None:
    """Redirected output has ==> phase headers, no ANSI codes, no carriage returns, only the final progress line, and each warning once."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = io.StringIO()

    _feed(ConsoleReporter(stream), _install_events())

    output = stream.getvalue()
    lines = output.splitlines()
    assert "\x1b" not in output and "\r" not in output
    assert "==> Fetch" in lines and "==> Verify" in lines
    assert [line for line in lines if "setup.exe:" in line] == ["    setup.exe: 74.0 MiB / 74.0 MiB (100%)"]
    assert lines.count("WARNING: checksum came from the vendor page") == 1
    assert "ERROR: verification failed" in lines
    assert "    ok: sha256 matches [vendor]" in lines
    assert lines[-3:] == ["==> Failed (exit code 3)", "update: failed", "Run log: C:\\opt\\.gupkg\\logs\\run.log"]


@pytest.mark.parametrize(
    "setting", [{"NO_COLOR": "1"}, {"TERM": "dumb"}], ids=["no-color", "dumb-terminal"]
)
def test_a_terminal_with_no_color_or_dumb_terminal_gets_the_plain_text_form(terminal, monkeypatch, setting) -> None:
    """A terminal that opts out of colour (NO_COLOR) or cannot draw (TERM=dumb) gets plain headers and no escape codes."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    for name, value in setting.items():
        monkeypatch.setenv(name, value)
    console, raw = terminal

    _feed(ConsoleReporter(console=console), _install_events())

    output = raw.getvalue()
    assert "\x1b" not in output and "\r" not in output
    assert "==> Fetch" in output.splitlines()
    assert output.count("setup.exe:") == 1


def test_the_standard_output_is_looked_up_at_each_write(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    """With no stream given the renderer writes to whatever standard output is current, so redirection by a caller is honoured."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    reporter = ConsoleReporter()

    reporter.handle(PhaseStart("Resolve"))

    assert capsys.readouterr().out == "==> Resolve\n"


def test_text_the_stream_cannot_encode_is_replaced_instead_of_failing_the_run() -> None:
    """Output redirected to a legacy code page never raises on characters outside it; they are replaced."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)

    reporter = ConsoleReporter(stream)
    reporter.handle(SubprocessLine("tool", "caf\u00e9 \u65e5\u672c done"))
    reporter.handle(Step("check \u2713 mark"))

    decoded = raw.getvalue().decode("cp1252")
    assert "caf\u00e9 ?? done" in decoded and "check ? mark" in decoded


def test_package_text_with_markup_and_escape_codes_is_shown_literally_on_a_terminal(terminal, monkeypatch) -> None:
    """Bracketed text from a package prints as written on a terminal rather than being interpreted as markup."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    console, _ = terminal

    _feed(ConsoleReporter(console=console), [
        Step("[bold red]not markup[/] and [link=http://x]text[/link]"),
        Warning("[/unclosed"),
    ])

    text = console.export_text()
    assert "[bold red]not markup[/] and [link=http://x]text[/link]" in text
    assert "WARNING: [/unclosed" in text


@pytest.mark.windows_integration
def test_real_child_process_output_is_shown_line_by_line_without_control_codes(monkeypatch) -> None:
    """Output of a real child (carriage-return progress, colour codes, non-ASCII text, an error stream) renders as clean separate lines."""
    from gupkg.system.windows import WindowsSystem

    monkeypatch.delenv("NO_COLOR", raising=False)
    program = (
        "import sys\n"
        "sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')\n"
        "sys.stdout.write('Receiving objects:  10%\\rReceiving objects: 100%\\n')\n"
        "sys.stdout.write('\\x1b[31mred text\\x1b[0m caf\u00e9\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('fatal: repository not found\\n')\n"
        "sys.exit(128)\n"
    )
    stream = io.StringIO()
    argv = [sys.executable, "-c", program]

    with use_reporter(ConsoleReporter(stream)):
        result = WindowsSystem().run(argv, on_line=subprocess_lines(argv))

    assert result.returncode == 128
    output = stream.getvalue()
    assert "\r" not in output and "\x1b" not in output
    body = [re.sub(r"^\s*\[python\] ", "", line) for line in output.splitlines()]
    assert "Receiving objects: 100%" in body
    assert "red text caf\u00e9" in body
    assert "fatal: repository not found" in body


def test_a_terminal_that_cannot_encode_the_marks_falls_back_to_plain_text(monkeypatch) -> None:
    """A terminal stream with a legacy encoding still shows every event: the renderer drops to plain text instead of failing."""
    from rich.console import Console

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    stream.isatty = lambda: True
    console = Console(file=stream, force_terminal=True, width=80, color_system=None, legacy_windows=False)

    reporter = ConsoleReporter(console=console)
    _feed(reporter, [PhaseStart("Fetch"), PhaseEnd("Fetch", "ok", 1.0), Warning("still visible"), RunEnd("summary", 0)])

    text = raw.getvalue().decode("cp1252")
    assert "WARNING: still visible" in text
    assert text.rstrip().endswith("summary")
    assert reporter.fancy is False


def test_warnings_and_errors_go_to_the_problem_stream_and_everything_else_to_the_main_one(monkeypatch) -> None:
    """With problems_to_stderr, plain output keeps phases and the summary on one stream and every warning and error, with its hint, on the other."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    main, problems = io.StringIO(), io.StringIO()

    _feed(ConsoleReporter(main, problems_to_stderr=True, problems_stream=problems), _install_events())

    lines = main.getvalue().splitlines()
    assert "==> Fetch" in lines and lines[-3] == "==> Failed (exit code 3)"
    assert not any("WARNING" in line or "ERROR" in line or "hint:" in line for line in lines)
    assert problems.getvalue().splitlines() == [
        "WARNING: checksum came from the vendor page",
        "ERROR: verification failed",
        "  hint: check the URL",
    ]


def test_problems_use_their_own_console_on_a_terminal(terminal, monkeypatch) -> None:
    """On a terminal, with problems_to_stderr, warnings and errors are drawn on the problem console and the rest on the main one."""
    from rich.console import Console

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    console, raw = terminal
    problem_raw = io.StringIO()
    problem_console = Console(record=True, force_terminal=True, width=100, color_system="truecolor", file=problem_raw)

    _feed(ConsoleReporter(console=console, problems_to_stderr=True, problems_console=problem_console), _install_events())

    problem_text, main_text = problem_console.export_text(), console.export_text()
    assert "WARNING: checksum came from the vendor page" in problem_text
    assert "ERROR: verification failed" in problem_text
    assert "WARNING" not in main_text and "ERROR" not in main_text
    assert "Fetch" in main_text


@pytest.mark.parametrize("mode", ["plain", "terminal"])
def test_escape_sequences_in_steps_warnings_and_errors_never_reach_the_output(terminal, monkeypatch, mode) -> None:
    """Package text with colour codes, cursor movement or a window-title request is shown without the control sequences, on both render paths."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    hostile = "\x1b[31mred\x1b[0m \x1b]0;owned\x07title \x1b[2J gone\rtail\x00"
    events = [Step(hostile), Warning(hostile), Error(hostile, hint=hostile), RunEnd(hostile, 0)]
    if mode == "plain":
        stream = io.StringIO()
        _feed(ConsoleReporter(stream), events)
        output = stream.getvalue()
    else:
        console, raw = terminal
        _feed(ConsoleReporter(console=console), events)
        output = console.export_text()
        # Rich adds its own styling codes; none of the hostile ones may appear.
        assert "\x1b]0" not in raw.getvalue() and "\x1b[2J" not in raw.getvalue()

    assert "\x1b[31m" not in output and "\x1b]0" not in output and "\x1b[2J" not in output
    assert "\x00" not in output and "\r" not in output
    assert "red title  gonetail" in output


def test_the_text_log_is_cleaned_like_the_console_while_the_json_log_keeps_the_original(tmp_path) -> None:
    """The text log never holds control sequences from package text; the JSONL keeps the original, safely escaped by JSON."""
    import json

    from gupkg.report.session import RunSession

    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[], fallback_base=tmp_path,
    )
    with session:
        from gupkg.report.events import emit

        emit(Step("\x1b[31mred\x1b[0m step"))
        session.finish("done", 0)

    [log] = (tmp_path / ".gupkg" / "logs").glob("2*.log")
    [events] = (tmp_path / ".gupkg" / "logs").glob("2*.jsonl")
    assert "\x1b" not in log.read_text(encoding="utf-8") and "red step" in log.read_text(encoding="utf-8")
    steps = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines() if '"step"' in line]
    assert steps[0]["text"] == "\x1b[31mred\x1b[0m step"
