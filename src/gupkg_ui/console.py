"""Show a batch on the console and ask its questions there: the output side of the non-interactive commands of the UI programs.

``gupkg-manager check-updates --all``, ``update --all`` and ``check`` (and the text printed after a UI program closes) write to a normal console.
This module provides the ``Sink`` that prints a batch's lines (in colour on a terminal, as plain text when output is redirected or ``NO_COLOR``
is set), the yes-or-no prompts for a plan and for a missing checksum, and the combined ``--json`` document of a batch, which has the shape of
the CLI's own result document (``"api": 1``) so a schedule reads both the same way.

Usage and API
-------------
``ConsoleSink(stream)`` is a ``Sink``. ``console_confirm(...)`` and ``console_ask_checksum(...)`` return the question callbacks for a person at a
terminal. ``outcome_document(command, outcome)`` builds the machine document; ``closing_text(outcome)`` returns the lines printed after a
program closes (the summary and the log paths).

Implementation Approach
-----------------------
Only ``rich.text.Text`` objects reach Rich, so brackets in package output stay literal. The prompts read one line from the given input stream and
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
    """
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
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
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._console = Console(file=stream, highlight=False, soft_wrap=True) if _colour(stream) else None

    def lines(self, lines: Sequence[Text]) -> None:
        """Print each line."""
        for line in lines:
            if self._console is not None:
                self._console.print(line, soft_wrap=True)
            else:
                self._stream.write(line.plain + "\n")
        self._stream.flush()

    def progress(self, text: str | None) -> None:
        """Ignore live progress: the lasting line of a finished transfer is printed by ``lines``."""

    def status(self, text: str) -> None:
        """Ignore the one-line state: the console shows the log itself."""


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


def console_confirm(stdin: TextIO, stream: TextIO):
    """Return the callback that asks once for a whole plan on the console (the plan itself was already printed)."""
    def confirm(lines: list[Text], question: str) -> bool:
        """Ask *question* and return the answer."""
        return _answer(question, stdin, stream)
    return confirm


def console_ask_checksum(stdin: TextIO, stream: TextIO):
    """Return the callback that asks, per package, whether to install it once without a checksum (the default is no)."""
    def ask(item: PlannedTarget) -> bool:
        """Show the package, version, URL and where the URL came from, then ask."""
        checksum = item.checksum
        stream.write(
            f"{literal(item.package or item.target, limit=80)} {literal(item.version or '?', limit=40)} has no checksum.\n"
            f"  url: {literal(checksum.url if checksum else '?')}\n  from: {literal(checksum.url_from if checksum else '?')}\n"
        )
        return _answer("Install it once without checking its checksum?", stdin, stream)
    return ask


def outcome_document(command: str, outcome: BatchOutcome) -> dict[str, Any]:
    """Return the ``--json`` document of a batch: the CLI's head (``api``, ``command``, ``ok``, ``exit_code``) and one entry per target.

    Parameters
    ----------
    command : str
        The command of the batch (``update``, ``check-updates``, ``check``).
    outcome : BatchOutcome
        The combined outcome.
    """
    document: dict[str, Any] = {
        "api": API_VERSION, "command": command, "ok": outcome.ok, "exit_code": outcome.exit_code,
        "cancelled": outcome.cancelled,
        "results": list(outcome.entries),
        "calls": [{"argv": list(call.argv), "exit_code": call.exit_code, "problems": list(call.problems)} for call in outcome.calls],
    }
    if outcome.problems:
        document["errors"] = [{"code": "call-failed", "message": literal(item)} for item in outcome.problems]
    return document


def closing_text(outcome: BatchOutcome) -> list[str]:
    """Return the lines to print after a UI program closes: the summary of the last run and where its logs are."""
    lines = summarize_entries(outcome.entries)
    logs = outcome.logs()
    lines += [f"log: {path}" for path in logs[:5]]
    if len(logs) > 5:
        lines.append(f"... and {len(logs) - 5} more logs")
    return lines
