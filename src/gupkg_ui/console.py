"""Show a batch on the console and ask its questions there: the output side of the non-interactive commands of the UI programs.

``gupkg-manager check-updates --all``, ``update --all`` and ``check`` (and the text printed after a UI program closes) write to a normal console.
This module provides the ``Sink`` that prints a batch's lines (in colour on a terminal, as plain text when output is redirected or ``NO_COLOR``
is set), the yes-or-no prompts for a plan and for a missing checksum, and the combined ``--json`` document of a batch, which has the shape of
the CLI's own result document (``"api": 2``) so a schedule parses both the same way.

Usage and API
-------------
``ConsoleSink(stream)`` is a ``Sink``. ``console_confirm(...)`` and ``console_ask_checksum(...)`` return the question callbacks for a person at a
terminal. ``outcome_document(command, outcome)`` builds the machine document; ``closing_text(outcome)`` returns the lines printed after a
program closes (the summary and the log paths).

Implementation Approach
-----------------------
Only ``rich.text.Text`` objects reach Rich, so brackets in package output stay literal. The prompts take one line from the given input stream and
treat anything but ``y`` or ``yes`` as no, so a closed or empty input is a refusal.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from gupkg.report.events import API_VERSION

from .plans import PlannedTarget
from .render import literal, summarize_entries
from .session import BatchOutcome


def configure_streams(*streams: TextIO) -> None:
    """Make writing to *streams* unable to crash: a redirected stream is UTF-8 and a character it cannot encode is replaced.

    The launcher runs ``python -I``, which ignores ``PYTHONIOENCODING``, so a pipe or a file would use the ANSI code page and fail on the
    heavy rules and marks of the output; a console keeps its own code page.

    Parameters
    ----------
    *streams : TextIO
        The streams to make safe, normally standard output and standard error.
    """
    # Reconfigure each stream that can be: a terminal only replaces what it cannot show, a pipe or file also becomes UTF-8.
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # A terminal keeps its code page and only gets the replacement rule.
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


class ConsoleSink:
    """A ``Sink`` that prints lasting lines to a text stream and ignores live progress (a console keeps no in-place line).

    Parameters
    ----------
    stream : TextIO
        Where the lines go. On a terminal (and without ``NO_COLOR``) the lines are drawn with their colours; anywhere else they are plain text.

    Methods
    -------
    lines
        Print each line.
    progress
        Ignore live progress.
    status
        Ignore the one-line state.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._console = Console(file=stream, highlight=False, soft_wrap=True) if _colour(stream) else None

    def lines(self, lines: Sequence[Text]) -> None:
        """Print each line.

        Parameters
        ----------
        lines : sequence of Text
            The lasting lines to print, drawn in colour on a terminal and as plain text elsewhere.
        """
        # Print through Rich on a colour terminal, otherwise write the plain text of each line.
        for line in lines:
            if self._console is not None:
                self._console.print(line, soft_wrap=True)
            else:
                self._stream.write(line.plain + "\n")
        self._stream.flush()

    def progress(self, text: str | None) -> None:
        """Ignore live progress, because the lasting line of a finished transfer is printed by ``lines``.

        Parameters
        ----------
        text : str or None
            The live progress text, which is dropped.
        """

    def status(self, text: str) -> None:
        """Ignore the one-line state, because the console shows the log itself.

        Parameters
        ----------
        text : str
            The state text, which is dropped.
        """


def console_confirm(stdin: TextIO, stream: TextIO):
    """Return the callback that asks once for a whole plan on the console (the plan itself was already printed).

    Parameters
    ----------
    stdin : TextIO
        Where the answer is read from.
    stream : TextIO
        Where the question is written.

    Returns
    -------
    callable
        ``confirm(lines, question)``, which answers whether the person agreed.
    """
    def _confirm(lines: list[Text], question: str) -> bool:
        """Ask *question* on the console and return whether the person agreed (*lines*, already printed, is unused)."""
        return _answer(question, stdin, stream)
    return _confirm


def console_ask_checksum(stdin: TextIO, stream: TextIO):
    """Return the callback that asks, per package, whether to install it once without a checksum (the default is no).

    Parameters
    ----------
    stdin : TextIO
        Where the answer is read from.
    stream : TextIO
        Where the question is written.

    Returns
    -------
    callable
        ``ask(item)``, which answers whether the package may be installed once without a checksum.
    """
    def _ask(item: PlannedTarget) -> bool:
        """Show the package, version, URL and where the URL came from, then return whether the unchecked install was approved."""
        # Say exactly what is about to be installed unchecked, from where, before asking.
        checksum = item.checksum
        stream.write(
            f"{literal(item.package or item.target, limit=80)} {literal(item.version or '?', limit=40)} has no checksum.\n"
            f"  url: {literal(checksum.url if checksum else '?')}\n  from: {literal(checksum.url_from if checksum else '?')}\n"
        )
        return _answer("Install it once without checking its checksum?", stdin, stream)
    return _ask


def outcome_document(command: str, outcome: BatchOutcome) -> dict[str, Any]:
    """Return the ``--json`` document of a batch: the CLI's head (``api``, ``command``, ``ok``, ``exit_code``) and one entry per target.

    Parameters
    ----------
    command : str
        The command of the batch (``update``, ``check-updates``, ``check``).
    outcome : BatchOutcome
        The combined outcome.

    Returns
    -------
    dict
        The head, one entry per target, the calls that were made and, when a call failed, ``errors``.
    """
    # Start from the CLI's head and the per-target entries, so a schedule reads this document like a CLI one.
    document: dict[str, Any] = {
        "api": API_VERSION, "command": command, "ok": outcome.ok, "exit_code": outcome.exit_code,
        "cancelled": outcome.cancelled,
        "results": list(outcome.entries),
        "calls": [{"argv": list(call.argv), "exit_code": call.exit_code, "problems": list(call.problems)} for call in outcome.calls],
    }

    # Add call-level errors only when a call failed, as the CLI does.
    if outcome.problems:
        document["errors"] = [{"code": "call-failed", "message": literal(item)} for item in outcome.problems]
    return document


def closing_text(outcome: BatchOutcome) -> list[str]:
    """Return the lines to print after a UI program closes: the summary of the last run and where its logs are.

    Parameters
    ----------
    outcome : BatchOutcome
        The last batch of the program.

    Returns
    -------
    list of str
        The summary lines followed by at most five log paths and a count of the rest.
    """
    # Summarize the entries, then name the first logs and count the rest.
    lines = summarize_entries(outcome.entries)
    logs = outcome.logs()
    lines += [f"log: {path}" for path in logs[:5]]
    if len(logs) > 5:
        lines.append(f"... and {len(logs) - 5} more logs")
    return lines


# --- supporting helpers ---


def _colour(stream: TextIO) -> bool:
    """Return whether *stream* is a terminal that may show colour (not redirected, ``NO_COLOR`` unset, ``TERM`` not ``dumb``)."""
    isatty = getattr(stream, "isatty", None)
    try:
        terminal = bool(isatty and isatty())
    except (OSError, ValueError):
        terminal = False
    return terminal and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def _answer(prompt: str, stdin: TextIO, stream: TextIO) -> bool:
    """Write *prompt*, read one line and return whether it is ``y`` or ``yes`` (nothing, a closed input or anything else is no)."""
    stream.write(f"{prompt} [y/N] ")
    stream.flush()
    try:
        line = stdin.readline()
    except (OSError, ValueError):
        return False
    return line.strip().lower() in ("y", "yes")
