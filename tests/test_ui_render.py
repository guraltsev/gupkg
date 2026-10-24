"""Cover how the UI programs draw a run: phase headers in order and in colour, literal text, bounded volume and the verdict at the end.

Behaviour covered: phase headers are bold cyan with a mark and word when a phase ends (check, cross, dash; a failure says ``failed``); text that comes from a package
(brackets that look like markup, escape sequences, bidirectional overrides) is shown literally and cleaned; a transfer shows a live progress line and leaves one lasting line
when done; past the detail limit lines are counted not shown and the count is reported once; the summary names every failed and skipped target and the verdict lines are
``Done`` or ``Failed (exit code N)``; an event of an unknown kind or with wrong types never raises.

Real: the renderer on records (including the recorded events of the real CLI). Mocked: nothing.
Out of scope: widgets (``test_ui_screens.py``).
"""

from __future__ import annotations

import json

import pytest

from gupkg_ui import render
from gupkg_ui.render import EventRenderer, progress_line, summarize_entries, verdict_lines
from tests.ui_support import entry, load_fixture


def plain(lines) -> list[str]:
    """Return the plain text of rendered lines."""
    return [line.plain for line in lines]


def test_phases_appear_in_order_with_colour_and_words(tmp_path) -> None:
    """The recorded package events give the headers Resolve..Finish in order, bold cyan, each ended by a green check or a dim dash with its word."""
    records = [json.loads(line) for line in load_fixture("events_alpha.jsonl", tmp_path).splitlines()]
    lines = EventRenderer().feed(records)

    headers = [line for line in lines if line.plain.startswith("━━ ")]
    names = [line.plain.split()[1] for line in headers]
    assert names == ["Resolve", "Fetch", "Verify", "Unpack", "Prepare", "Activate", "Integrate", "Finish"]
    assert all("cyan" in str(line.style) and "bold" in str(line.style) for line in headers)
    ends = {line.plain.strip().split()[1]: line for line in lines if line.plain.strip().startswith(("✓", "–", "✗"))}
    assert "green" in str(ends["Resolve"].style) and "dim" in str(ends["Fetch"].style)
    assert "skipped" in ends["Fetch"].plain


def test_a_failed_phase_says_failed_in_words_and_red() -> None:
    """Colour is never the only signal: a failed phase carries a cross and the word."""
    lines = EventRenderer().feed([{"event": "phase_end", "phase": "Verify", "status": "failed", "elapsed": 1.25}])

    assert plain(lines) == ["  ✗ Verify failed  1.2s"] and "red" in str(lines[0].style)


def test_text_from_a_package_is_literal_and_cleaned() -> None:
    """Brackets that look like markup stay as typed; escape sequences, control characters and bidirectional overrides are removed."""
    hostile = "[bold red]boom[/] \x1b[31mred\x1b[0m ‮evil\x07 [update.check]"
    lines = EventRenderer().feed([
        {"event": "step", "text": hostile, "kind": "info"},
        {"event": "warning", "text": hostile},
        {"event": "error", "text": hostile, "hint": hostile},
        {"event": "subprocess_line", "cmd": "git", "line": hostile},
        {"event": "package_start", "view": {"name": hostile}},
    ])

    text = "\n".join(plain(lines))
    assert "[bold red]boom[/]" in text and "[update.check]" in text
    assert "\x1b" not in text and "‮" not in text and "\x07" not in text
    assert any(line.plain.startswith("Warning: ") for line in lines) and any(line.plain.startswith("Error: ") for line in lines)


def test_a_transfer_shows_live_progress_and_one_lasting_line() -> None:
    """While a transfer runs only the live text changes; when it completes the log gets one lasting line and the live text clears."""
    renderer = EventRenderer()

    assert renderer.feed([{"event": "progress", "task": "tool.zip", "done": 512, "total": 2048, "unit": "B"}]) == []
    assert "tool.zip" in renderer.progress_text() and "25%" in renderer.progress_text()
    lasting = renderer.feed([{"event": "progress", "task": "tool.zip", "done": 2048, "total": 2048, "unit": "B"}])

    assert plain(lasting) == ["    ✓ tool.zip  [████████████████████]  2.0 KiB / 2.0 KiB  100%"]
    assert renderer.progress_text() is None


def test_progress_with_untrusted_numbers_never_raises() -> None:
    """Negative, text or boolean numbers give a readable line, not an exception."""
    assert "unknown" in progress_line("t", "many", 10, "B")
    assert "received" in progress_line("t", 5, 0, "B")
    assert render.format_bytes(-1) == "?" and render.format_bytes(True) == "?"


def test_detail_lines_past_the_limit_are_counted_not_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runaway stream of detail lines is bounded; the omitted count is reported once, and headers and errors are still shown."""
    monkeypatch.setattr(render, "MAX_DETAIL_LINES", 5)
    renderer = EventRenderer()
    records = [{"event": "subprocess_line", "cmd": "x", "line": str(number)} for number in range(50)] + [{"event": "error", "text": "late failure"}]

    shown = plain(renderer.feed(records))

    assert len([line for line in shown if "│" in line]) == 5 and "Error: late failure" in shown
    assert plain(renderer.finish()) == ["    … 45 more detail lines are in the log, not shown here"] and renderer.finish() == []


def test_unknown_events_and_wrong_types_are_ignored_not_raised() -> None:
    """A newer gupkg may add event kinds, and a hostile file may put wrong types in fields; the renderer shows what it understands."""
    lines = EventRenderer().feed([
        {"event": "from_the_future", "x": 1}, {"event": 12}, {"event": "phase_start", "phase": 7},
        {"event": "phase_end", "phase": None, "status": None, "elapsed": "soon"}, {"event": "package_end", "result": "nope"},
    ])

    assert all(isinstance(text, str) for text in plain(lines))


def test_the_summary_names_every_failed_and_skipped_target_and_the_verdict_comes_last() -> None:
    """One line per target, a failure count, and a closing header that says Done or Failed with the exit code."""
    entries = [
        entry("alpha"), entry("beta", ok=False, status="failed", code=3, error={"code": "x", "message": "boom"}),
        entry("gamma", status="skipped"), entry("delta", status="refused", error={"code": "refused", "message": "refused: no checksum"}),
    ]
    summary = summarize_entries(entries)
    lines = plain(verdict_lines(3, summary))

    assert summary[0] == "ok alpha 2.0.0: updated (was 1.0.0)"
    assert "x beta: update failed: boom" in summary and "- gamma: skipped" in summary
    assert any(line.startswith("- delta: refused") for line in summary) and summary[-1] == "1 of 4 failed: beta"
    assert lines[0].startswith("━━ Failed (exit code 3)") and lines[-1] == "    1 of 4 failed: beta"
    assert plain(verdict_lines(0, ["ok"], plain=True))[0] == "==> Done"
