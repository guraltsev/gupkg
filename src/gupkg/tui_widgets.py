"""Provide Textual widgets shared by the package and manager interfaces.

Importing this module imports Textual, so the interface modules import it only
inside their ``run_*`` entry points.

Usage and API
-------------
``OutputView`` shows plain text that follows new lines until the user scrolls
up. Call ``set_lines(lines)`` to replace its content.

Implementation Approach
-----------------------
The view is an anchored ``VerticalScroll`` holding one markup-free ``Static``.
Anchoring keeps the bottom visible as content grows and releases when the user
scrolls away, as a terminal does.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Static

# Color carries meaning, never decoration: red for errors, yellow for
# warnings, green for good or actionable news, dim for settled or neutral state.
# Every colored item also keeps its words, so nothing depends on color alone.
_LINE_STYLES = (
    (("ERROR:", "  Error:"), "$error"),
    (("WARNING:",), "$warning"),
    (("Available:",), "$success"),
)
_UPDATE_STYLES = {
    "available": "$success",
    "error": "$error",
    "current": "dim",
    "not-configured": "dim",
    "unchecked": "dim",
}


class OutputView(VerticalScroll):
    """Scrollable plain-text output that follows new lines until the user scrolls up."""

    can_focus = True

    def compose(self) -> ComposeResult:
        """Compose the single markup-free text widget."""
        yield Static("", markup=False, id="output-text")

    def on_mount(self) -> None:
        """Follow the bottom of the output as it grows."""
        self.anchor()

    def set_lines(self, lines: list[str | Content]) -> None:
        """Replace the displayed text with *lines* (rendered literally).

        Plain strings are colored by their leading message kind; ``Content``
        items keep the styling they were built with.
        """
        shown = [
            item if isinstance(item, Content) else style_line(line)
            for item in lines
            for line in ([item] if isinstance(item, Content) else item.split("\n"))
        ]
        self.query_one("#output-text", Static).update(Content("\n").join(shown))


def style_line(line: str) -> Content:
    """Return *line* as literal text, colored by the kind of message it begins with."""
    for prefixes, style in _LINE_STYLES:
        if line.startswith(prefixes):
            return Content.styled(line, style)
    return Content(line)


def update_style(status: str) -> str:
    """Return the style that marks a target's update status."""
    return _UPDATE_STYLES.get(status, "")
