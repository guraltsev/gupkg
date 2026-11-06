"""The prompts of the UI programs as full-screen dialogs: confirmations, the checksum question, the cancel question, the key help.

Prompts are modals (design section 15): the CLI is always called with ``--no-input``, so every question is asked here before the call and the answer is
carried by ``--yes`` or by the checksum decision. A dialog answers only to explicit keys (``y`` for yes, ``n`` or Escape for no), never to Enter, so that a
key pressed in passing cannot confirm anything, and its default is always the safe answer. Each dialog carries the same key-hint line and status line as every other screen.

Usage and API
-------------
Push a ``ConfirmScreen`` and await ``app.push_screen_wait(...)`` from a worker: it returns ``True`` or ``False``. ``checksum_dialog(item)`` builds the
question for a package whose checksum is missing, ``cancel_dialog()`` the question that Escape asks during a run. ``HelpScreen`` shows the keys of a program.

Implementation Approach
-----------------------
The body is the question on top (always visible) and the supporting lines below it in a scrolling area, so a long plan scrolls under a question that stays in view.
All text is built as ``rich.text.Text`` from cleaned strings, never parsed as markup.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from ..plans import PlannedTarget
from ..render import literal
from .frame import ModalFrame


class ConfirmScreen(ModalFrame):
    """Ask a yes-or-no question; ``y`` answers yes, ``n`` and Escape answer no, and the dialog returns the boolean.

    Parameters
    ----------
    question : str
        The question, always visible at the top.
    lines : sequence of Text, optional
        Supporting lines (the plan table, the package's details) in a scrolling area under the question.

    Methods
    -------
    key_hints
        Return the keys of the dialog.
    status_text
        Return the program's status line.
    compose_body
        Yield the question and the scrolling details.
    action_answer
        Close the dialog with the answer.

    Attributes
    ----------
    BINDINGS : list of Binding
        The keys of the screen, as ``key`` (label): ``y`` (Yes), ``n`` (No), ``escape`` (No).
    """

    DEFAULT_CSS = """
    ConfirmScreen #question { height: auto; padding: 0 1; text-style: bold; }
    ConfirmScreen #details { height: 1fr; padding: 0 1; }
    """
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n", "answer(False)", "No"), Binding("escape", "answer(False)", "No")]

    def __init__(self, question: str, lines: Sequence[Text] = ()) -> None:
        super().__init__()
        self._question = literal(question, limit=400)
        self._lines = list(lines)

    def key_hints(self) -> str:
        """Return the keys of the dialog.

        Returns
        -------
        str
            The text of the top line.
        """
        return "y yes   n no   Esc no   ↑↓ PgUp PgDn scroll"

    def status_text(self) -> str:
        """Return the program's status line.

        Returns
        -------
        str
            The text of the bottom line.
        """
        return str(getattr(self.app, "status_line", lambda: "")())

    def compose_body(self) -> ComposeResult:
        """Yield the question and the scrolling details."""
        body = Text("\n").join(self._lines) if self._lines else Text("")
        with Vertical():
            yield Static(Text(self._question), id="question", markup=False)
            with VerticalScroll(id="details"):
                yield Static(body, markup=False)

    def action_answer(self, value: bool) -> None:
        """Close the dialog with the answer.

        Parameters
        ----------
        value : bool
            The answer to return: ``True`` for yes.
        """
        self.dismiss(value)


def checksum_dialog(item: PlannedTarget) -> ConfirmScreen:
    """Return the question for a package whose checksum is missing: package, version, URL and where the URL came from (the answer is never stored).

    Parameters
    ----------
    item : PlannedTarget
        The planned target that has no checksum.

    Returns
    -------
    ConfirmScreen
        The dialog to push; it returns ``True`` when the person agrees to install without a checksum once.
    """
    # Show where the download comes from, then why it cannot be checked.
    checksum = item.checksum
    lines = [
        Text(f"package: {literal(item.package or item.target, limit=120)} {literal(item.version or '?', limit=60)}"),
        Text(f"url: {literal(checksum.url if checksum else '?')}"),
        Text(f"url comes from: {literal(checksum.url_from if checksum else '?')}"),
        Text(""),
        Text("The download has no SHA-256, so it cannot be checked. Installing it once is recorded in the receipt and flagged by `gupkg check`."),
    ]
    return ConfirmScreen("Install it once without a checksum?  (default: no)", lines)


def cancel_dialog() -> ConfirmScreen:
    """Return the question that Escape asks during a run (decision D37): cancelling never leaves a package half-done.

    Returns
    -------
    ConfirmScreen
        The dialog to push; it returns ``True`` when the person wants to cancel after the current package.
    """
    return ConfirmScreen("Cancel after the current package?", [
        Text("The package that is running now finishes cleanly; the packages after it are skipped and left unchanged."),
    ])


class HelpScreen(ModalFrame):
    """Show the keys and ideas of a program; Escape, ``q`` and ``?`` close it.

    Parameters
    ----------
    title : str
        The heading of the help.
    lines : sequence of str
        The help text, one line per item.

    Methods
    -------
    key_hints
        Return the keys of the help screen.
    status_text
        Return the program's status line.
    compose_body
        Yield the help text in a scrolling area.
    action_close
        Return to the screen below.

    Attributes
    ----------
    BINDINGS : list of Binding
        The keys of the screen, as ``key`` (label): ``escape`` (Close), ``q`` (Close), ``question_mark`` (Close).
    """

    DEFAULT_CSS = "HelpScreen #help { padding: 0 1; }"
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close"), Binding("question_mark", "close", "Close")]

    def __init__(self, title: str, lines: Sequence[str]) -> None:
        super().__init__()
        self._title = literal(title, limit=200)
        self._lines = [literal(line) for line in lines]

    def key_hints(self) -> str:
        """Return the keys of the help screen.

        Returns
        -------
        str
            The text of the top line.
        """
        return "Esc close   ↑↓ PgUp PgDn scroll"

    def status_text(self) -> str:
        """Return the program's status line.

        Returns
        -------
        str
            The text of the bottom line.
        """
        return str(getattr(self.app, "status_line", lambda: "")())

    def compose_body(self) -> ComposeResult:
        """Yield the help text in a scrolling area."""
        with VerticalScroll(id="help"):
            yield Static(Text("\n".join([self._title, "", *self._lines])), markup=False)

    def action_close(self) -> None:
        """Return to the screen below."""
        self.dismiss(None)
