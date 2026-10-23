"""Cover how the TUI output view marks error lines while keeping package text literal.

Textual's real test driver is used. Console colouring (terminal, redirected output
and NO_COLOR) is covered in ``test_console_renderer.py``; exact palette values are
out of scope.
"""

from __future__ import annotations

import asyncio


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
