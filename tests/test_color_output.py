"""Cover when console output is colored and which messages are marked.

Real streams and Textual's test driver are used; only the console check is
simulated with a stream that reports itself as a terminal. Exact palette
values and Windows console mode switching are out of scope.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from gupkg.core import colorize, log_error, log_info


class _Terminal(io.StringIO):
    """Provide an in-memory stream that reports being an interactive terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def color_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the platform console already understands ANSI colors."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr("gupkg.core.enable_virtual_terminal", lambda _fileno: True)
    monkeypatch.setattr(_Terminal, "fileno", lambda self: 1, raising=False)


def test_redirected_output_is_never_colored() -> None:
    """Text written to a non-terminal stream has no escape codes."""
    assert colorize("ERROR: boom", "error", io.StringIO()) == "ERROR: boom"


def test_terminal_output_colors_errors_and_respects_no_color(
    monkeypatch: pytest.MonkeyPatch, color_capable: None
) -> None:
    """A terminal gets colored errors, unless NO_COLOR is set."""
    terminal = _Terminal()
    assert colorize("ERROR: boom", "error", terminal).startswith("\x1b[")
    monkeypatch.setenv("NO_COLOR", "1")
    assert colorize("ERROR: boom", "error", terminal) == "ERROR: boom"


def test_logged_errors_are_colored_but_information_is_not(
    monkeypatch: pytest.MonkeyPatch, color_capable: None
) -> None:
    """On a terminal, error log lines are colored while ordinary information stays plain."""
    import contextlib

    terminal = _Terminal()
    with contextlib.redirect_stdout(terminal):
        log_info("fine")
        log_error("boom")

    lines = terminal.getvalue().splitlines()
    assert lines[0] == "fine"
    assert lines[1].startswith("\x1b[") and "ERROR: boom" in lines[1]


def test_tui_output_marks_errors_and_keeps_text_literal() -> None:
    """Error lines in the output view carry a style while all text stays literal."""
    from textual.app import App
    from textual.widgets import Static

    from gupkg.tui_widgets import OutputView

    class Host(App):
        def compose(self):
            yield OutputView()

    async def drive() -> None:
        app = Host()
        async with app.run_test() as pilot:
            app.query_one(OutputView).set_lines(["fine [x]", "ERROR: bad [/y]"])
            await pilot.pause()
            content = app.query_one("#output-text", Static).render()
            assert content.plain == "fine [x]\nERROR: bad [/y]"
            assert [span.style for span in content.spans] == ["$error"]

    asyncio.run(drive())
