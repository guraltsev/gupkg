"""Render a run's events as console text, in colour on a terminal and as plain text elsewhere.

On a terminal the renderer draws bold coloured phase rules with a check mark
or a cross and the elapsed time, dimmed indented steps, a transient progress
bar for downloads, yellow warnings and red errors. When output is redirected,
when ``NO_COLOR`` is set, or when ``TERM`` is ``dumb``, it prints plain text:
``==>`` phase headers, no ANSI codes, no carriage returns, and only the final
progress line of each transfer. The closing summary is always the last thing
printed.

Usage and API
-------------
Create ``ConsoleReporter(stream=None)`` and give it to the run (see
``gupkg.report.events.use_reporter``). ``stream`` defaults to the current
standard output; pass ``sys.stderr`` to keep standard output for a
machine-readable document. ``PlainFormatter`` turns events into the plain
text lines that the redirected console, the text log and the TUI all show;
``LineReporter`` hands those lines (and live progress text) to callbacks, for
a UI that draws its own widgets.

Implementation Approach
-----------------------
One ``handle`` method dispatches on the event type under a lock, because child
processes report from reader threads. Rich renders only in terminal mode, and
only ``Text`` objects reach it, so brackets in package output stay literal.
The plain path never raises on text its stream cannot encode: unencodable
characters are replaced.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any, Callable, TextIO

from .events import (
    Error, PackageEnd, PackageStart, PhaseEnd, PhaseStart, Progress, Prompt, RunEnd,
    RunStart, Step, SubprocessLine, Warning,
)


# ---------------------------------------------------------------------------
# Text shared by every renderer
# ---------------------------------------------------------------------------


def format_bytes(value: int) -> str:
    """Render a byte count compactly, for example ``74.0 MiB``."""
    if value < 1024:
        return f"{value} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB"):
        amount /= 1024
        if amount < 1024:
            return f"{amount:.1f} {unit}"
    return f"{amount / 1024:.1f} TiB"


def format_progress(event: Progress) -> str:
    """Render one progress event as the text line shown for a finished or plain transfer."""
    # Byte transfers read as sizes; any other unit is shown as plain counts.
    show = format_bytes if event.unit == "B" else (lambda n: f"{n} {event.unit}")
    if event.total is None:
        return f"{event.task}: {show(event.done)} received"
    percentage = min(event.done / event.total * 100, 100) if event.total else 100
    return f"{event.task}: {show(event.done)} / {show(event.total)} ({percentage:.0f}%)"


def _elapsed(seconds: float) -> str:
    """Render a duration as ``12.3s``."""
    return f"{seconds:.1f}s"


class PlainFormatter:
    """Turn events into plain text lines: ASCII structure, no colour, no carriage returns.

    Progress events are held back and only the last one of each transfer is
    produced: when the transfer completes, or when any other event follows it.
    """

    def __init__(self) -> None:
        self._pending: dict[str, Progress] = {}

    def feed(self, event: Any) -> list[str]:
        """Return the lines *event* adds (including any held progress line it releases)."""
        # A held progress line is final as soon as anything else is reported.
        if isinstance(event, Progress):
            return self._feed_progress(event)
        lines = self.finish()

        if isinstance(event, RunStart):
            lines.append(f"gupkg {event.command} ({event.mode}{', elevated' if event.elevated else ''})")
        elif isinstance(event, PackageStart):
            lines.append(f"Package: {event.view.get('name', '?')}  ({event.view.get('path', '')})")
        elif isinstance(event, PhaseStart):
            lines.append(f"==> {event.phase}")
        elif isinstance(event, PhaseEnd):
            lines.append(f"    {event.phase}: {event.status} ({_elapsed(event.elapsed)})")
        elif isinstance(event, Step):
            prefix = {"ok": "ok: ", "add": "+ ", "remove": "- "}.get(event.kind, "")
            lines.extend(f"    {prefix if index == 0 else ''}{text}" for index, text in enumerate(event.text.splitlines() or [""]))
        elif isinstance(event, SubprocessLine):
            lines.append(f"      [{event.cmd}] {event.line}")
        elif isinstance(event, Prompt):
            lines.append(f"? {event.text}")
        elif isinstance(event, Warning):
            lines.append(f"WARNING: {event.text}")
        elif isinstance(event, Error):
            lines.append(f"ERROR: {event.text}")
            if event.hint:
                lines.append(f"  hint: {event.hint}")
        elif isinstance(event, PackageEnd):
            result = event.result
            status = result.get("status") or ("ok" if result.get("ok", True) else "failed")
            lines.append(f"Package {result.get('name', '?')}: {status}")
        elif isinstance(event, RunEnd):
            lines.append("==> Done" if event.exit_code == 0 else f"==> Failed (exit code {event.exit_code})")
            lines.extend(event.summary.splitlines())
        return lines

    def finish(self) -> list[str]:
        """Release every held progress line (a transfer that ended without completing)."""
        lines = [format_progress(item) for item in self._pending.values()]
        self._pending.clear()
        return lines

    def _feed_progress(self, event: Progress) -> list[str]:
        """Hold *event*, or produce its line at once when the transfer is complete."""
        if event.total is not None and event.done >= event.total and event.total > 0:
            self._pending.pop(event.task, None)
            return [f"    {format_progress(event)}"]
        self._pending[event.task] = event
        return []


# ---------------------------------------------------------------------------
# The console reporter
# ---------------------------------------------------------------------------


class ConsoleReporter:
    """Print a run's events to a stream, in colour on a terminal and as plain text otherwise.

    Parameters
    ----------
    stream : TextIO, optional
        Destination; ``None`` means the current ``sys.stdout`` at each write,
        so output redirected later is still honoured.
    console : rich.console.Console, optional
        A prepared Rich console (tests record with one); when given it decides
        the destination and whether the output is a terminal.
    """

    def __init__(self, stream: TextIO | None = None, *, console: Any = None) -> None:
        self._stream = stream
        self._lock = threading.RLock()
        self._plain = PlainFormatter()
        self._tasks: dict[str, Any] = {}
        self._progress: Any = None

        # Decide the mode once: colour only for a real terminal that has not
        # opted out through NO_COLOR or TERM=dumb.
        if console is not None and stream is None:
            self._stream = console.file
        probe = console.file if console is not None else (stream or sys.stdout)
        is_terminal = console.is_terminal if console is not None else _is_terminal(probe)
        self.fancy = bool(is_terminal) and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self._console = None
        if self.fancy:
            from rich.console import Console

            self._console = console or Console(file=stream, highlight=False)

    def handle(self, event: Any) -> None:
        """Render one event."""
        with self._lock:
            # A terminal whose encoding cannot draw the marks falls back to
            # plain text for the rest of the run instead of losing output.
            if self.fancy:
                try:
                    self._handle_fancy(event)
                    return
                except UnicodeEncodeError:
                    self.fancy = False
                    self._drop_unfinished_progress()
            for line in self._plain.feed(event):
                self._write_plain(line)

    # -- plain ---------------------------------------------------------------

    def _write_plain(self, line: str) -> None:
        """Write one line, replacing characters the stream cannot encode, and flush."""
        stream = self._stream or sys.stdout
        text = line + "\n"
        try:
            stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(text.encode(encoding, "replace").decode(encoding))
        stream.flush()

    # -- terminal ------------------------------------------------------------

    def _handle_fancy(self, event: Any) -> None:
        """Draw one event with Rich."""
        from rich.rule import Rule
        from rich.text import Text

        console = self._console
        if isinstance(event, Progress):
            self._draw_progress(event)
            return
        self._drop_unfinished_progress()

        if isinstance(event, RunStart):
            console.print(Text(f"gupkg {event.command}  ·  {event.mode}" + ("  ·  elevated" if event.elevated else ""), style="bold"))
        elif isinstance(event, PackageStart):
            console.print(Text(f"Package {event.view.get('name', '?')}", style="bold magenta"))
        elif isinstance(event, PhaseStart):
            console.print(Rule(Text(event.phase, style="bold cyan"), characters="━", style="cyan", align="left"))
        elif isinstance(event, PhaseEnd):
            mark, style = {"ok": ("✓", "bold green"), "failed": ("✗", "bold red")}.get(event.status, ("–", "dim"))
            label = event.phase if event.status == "ok" else f"{event.phase} {event.status}"
            console.print(Text(f"  {mark} {label}  {_elapsed(event.elapsed)}", style=style))
        elif isinstance(event, Step):
            mark, style = {
                "ok": ("✓ ", "green"), "add": ("+ ", "green"), "remove": ("− ", "red"),
            }.get(event.kind, ("", "dim"))
            for index, text in enumerate(event.text.splitlines() or [""]):
                console.print(Text(f"    {mark if index == 0 else ''}{text}", style=style))
        elif isinstance(event, SubprocessLine):
            console.print(Text(f"      │ {event.line}", style="dim"))
        elif isinstance(event, Prompt):
            console.print(Text(f"? {event.text}", style="bold"))
        elif isinstance(event, Warning):
            console.print(Text(f"WARNING: {event.text}", style="yellow"))
        elif isinstance(event, Error):
            console.print(Text(f"ERROR: {event.text}", style="bold red"))
            if event.hint:
                console.print(Text(f"  hint: {event.hint}", style="dim"))
        elif isinstance(event, PackageEnd):
            result = event.result
            status = result.get("status") or ("ok" if result.get("ok", True) else "failed")
            style = "green" if result.get("ok", True) else "red"
            console.print(Text(f"Package {result.get('name', '?')}: {status}", style=style))
        elif isinstance(event, RunEnd):
            succeeded = event.exit_code == 0
            title = "Done" if succeeded else f"Failed (exit code {event.exit_code})"
            console.print(Rule(Text(title, style="bold green" if succeeded else "bold red"), characters="━", style="green" if succeeded else "red", align="left"))
            for line in event.summary.splitlines():
                console.print(Text(f"    {line}"))

    def _draw_progress(self, event: Progress) -> None:
        """Update the transient bar, and print a lasting line when the transfer completes."""
        from rich.progress import (
            BarColumn, DownloadColumn, MofNCompleteColumn, Progress as RichProgress,
            TextColumn, TransferSpeedColumn,
        )
        from rich.text import Text

        # Start the bar lazily; it is transient, so it disappears when stopped.
        if self._progress is None:
            counts = DownloadColumn(binary_units=True) if event.unit == "B" else MofNCompleteColumn()
            columns = [TextColumn("  {task.description}"), BarColumn(), counts]
            if event.unit == "B":
                columns.append(TransferSpeedColumn())
            self._progress = RichProgress(*columns, console=self._console, transient=True, auto_refresh=False)
            self._progress.start()
        if event.task not in self._tasks:
            self._tasks[event.task] = self._progress.add_task(event.task, total=event.total)
        task_id = self._tasks[event.task]
        self._progress.update(task_id, completed=event.done, total=event.total, refresh=True)

        # A completed transfer leaves one lasting line in place of its bar.
        if event.total is not None and event.done >= event.total and event.total > 0:
            self._progress.remove_task(task_id)
            del self._tasks[event.task]
            if not self._tasks:
                self._progress.stop()
                self._progress = None
            self._console.print(Text(f"    ✓ {format_progress(event)}", style="green"))

    def _drop_unfinished_progress(self) -> None:
        """Remove a bar whose transfer ended without completing, before other text prints."""
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._tasks.clear()


class LineReporter:
    """Hand a run's events to callbacks as plain text lines and live progress text.

    Parameters
    ----------
    on_line : callable
        Receives every committed line (see ``PlainFormatter``).
    on_transient : callable, optional
        Receives the latest text of a transfer that is still running, which a
        UI shows in place and replaces; the transfer's final line arrives
        through ``on_line``.
    """

    def __init__(self, on_line: Callable[[str], None], on_transient: Callable[[str], None] | None = None) -> None:
        self._on_line = on_line
        self._on_transient = on_transient
        self._plain = PlainFormatter()
        self._lock = threading.RLock()

    def handle(self, event: Any) -> None:
        """Pass the lines (or the transient text) that *event* produces to the callbacks."""
        with self._lock:
            lines = self._plain.feed(event)
            for line in lines:
                self._on_line(line)
            if isinstance(event, Progress) and not lines and self._on_transient is not None:
                self._on_transient(format_progress(event))


def _is_terminal(stream: Any) -> bool:
    """Return whether *stream* is an interactive terminal."""
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty and isatty())
    except (OSError, ValueError):
        return False
