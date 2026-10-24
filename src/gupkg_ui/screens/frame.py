"""The fixed frame of every screen of the UI programs: a key-hint line on top, a status line at the bottom, a frameless body between them.

The revised style guide (``_docs/tui_style_guide.md``) requires both lines on every screen: the top line lists the keys that work *now*, the bottom line
says what is going on (counts, registry revision and age, the state of a run, the log path). Both are one line, never wrap, and clip with an ellipsis;
the content that matters most comes first so that a narrow terminal loses only the tail. Nothing in the frame relies on colour, and all text that
comes from packages, the network or logs is shown literally (never as markup).

Usage and API
-------------
Subclass ``FrameScreen``: implement ``compose_body()`` (the widgets between the lines), ``key_hints()`` (the text of the top line) and
``status_text()`` (the bottom line), and call ``refresh_frame()`` when either changes. ``ModalFrame`` is the same frame for a dialog screen.

Implementation Approach
-----------------------
The two lines are ``Static`` widgets docked to the top and bottom of the screen with ``markup=False``, so the body takes the remaining height and scrolls
by itself. Dialogs are full-screen modal screens that carry the same two lines, so no screen of either program is ever without them.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from ..render import literal

# CSS shared by every screen: no borders or padding, a one-line hint bar and status line that clip with an ellipsis.
FRAME_CSS = """
Screen { padding: 0; }
KeyHintBar, StatusFooter {
    height: 1; width: 100%; padding: 0 1;
    text-wrap: nowrap; text-overflow: ellipsis; overflow: hidden;
    background: $panel; color: $text;
}
KeyHintBar { dock: top; }
StatusFooter { dock: bottom; }
VerticalScroll, RichLog, DataTable, Input, OptionList { background: transparent; border: none; outline: none; }
"""


class KeyHintBar(Static):
    """The one-line list of keys that work on the current screen."""

    def __init__(self, text: str = "") -> None:
        super().__init__(Text(literal(text, limit=400), no_wrap=True, overflow="ellipsis"), markup=False)

    def show(self, text: str) -> None:
        """Replace the hints."""
        self.update(Text(literal(text, limit=400), no_wrap=True, overflow="ellipsis"))


class StatusFooter(Static):
    """The one-line status of the program: counts, registry revision and age, run state and the log path."""

    def __init__(self, text: str = "") -> None:
        super().__init__(Text(literal(text, limit=600), no_wrap=True, overflow="ellipsis"), markup=False)

    def show(self, text: str) -> None:
        """Replace the status."""
        self.update(Text(literal(text, limit=600), no_wrap=True, overflow="ellipsis"))


class _Framed:
    """Behaviour shared by the two screen kinds: compose the frame around ``compose_body`` and refresh it."""

    def key_hints(self) -> str:  # pragma: no cover - overridden
        """Return the text of the top line."""
        return ""

    def status_text(self) -> str:  # pragma: no cover - overridden
        """Return the text of the bottom line."""
        return ""

    def compose_body(self) -> ComposeResult:  # pragma: no cover - overridden
        """Yield the widgets between the two lines."""
        yield from ()

    def compose(self) -> ComposeResult:
        """Yield the hint line, the body and the status line."""
        yield KeyHintBar(self.key_hints())
        yield from self.compose_body()
        yield StatusFooter(self.status_text())

    def refresh_frame(self) -> None:
        """Show the current hints and status (call after anything that changes them)."""
        try:
            hints, status = self.query_one(KeyHintBar), self.query_one(StatusFooter)
        except NoMatches:
            return
        hints.show(self.key_hints())
        status.show(self.status_text())


class FrameScreen(_Framed, Screen):
    """A screen with the fixed frame."""


class ModalFrame(_Framed, ModalScreen):
    """A dialog screen with the fixed frame (it covers the whole terminal, so the lines never disappear)."""
