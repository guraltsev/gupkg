"""Turn the event records of a ``gupkg`` run into phase-styled Rich text, and results into the closing verdict.

The renderer shows what the console renderer of ``gupkg`` shows (design section 14.2): bold coloured phase headers (cyan
while running, then a green check, a red cross or a dim dash), indented detail lines, yellow warnings and red errors, a
progress line for transfers, and a verdict and summary that the caller writes last. It never uses markup: every piece of text
comes from a package, the network or a hook, so it is stripped of escape sequences, control characters and bidirectional
overrides and put into a ``rich.text.Text`` as literal characters. Nothing relies on colour alone (a failure also says
``failed`` and a warning starts with ``Warning:``).

Usage and API
-------------
``EventRenderer().feed(records)`` returns the ``Text`` lines that a batch of records adds; ``renderer.progress_text()`` is the
live line of a transfer that is still running (``None`` when none is); ``renderer.finish()`` returns a closing note when output
was left out. ``verdict_lines(exit_code, summary_lines)`` returns the header and indented summary that end a log, and
``summarize_entries(entries)`` builds the per-target summary lines from result entries.

Implementation Approach
-----------------------
One pass over the records, keeping only the state a display needs (the open phase, the transfer that is running, how many lines
were left out). Volume is bounded so that a hostile or runaway event stream cannot freeze the display: a line is cut at
``MAX_LINE_CHARS`` and, past ``MAX_DETAIL_LINES`` detail lines (steps and child-process output), only a count of the omitted
lines is kept, while headers, warnings, errors and results are always shown.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from rich.text import Text

from gupkg.report.events import clean_line

# The longest line that is shown; a longer one is cut and ends with an ellipsis.
MAX_LINE_CHARS = 2000
# Detail lines (steps and child-process output) shown per renderer; the rest is counted, never kept.
MAX_DETAIL_LINES = 10000
# Width of the filled part of a progress bar.
BAR_WIDTH = 20
# Style of a phase mark by the status the phase ended with: (mark, style).
_PHASE_END = {"ok": ("✓", "bold green"), "failed": ("✗", "bold red")}
# Mark and style of a step by its kind.
_STEP = {"ok": ("✓ ", "green"), "add": ("+ ", "green"), "remove": ("− ", "red")}


def literal(value: Any, *, limit: int = MAX_LINE_CHARS) -> str:
    """Return *value* as text that is safe to show: no escape sequences, control characters or bidirectional overrides, cut at *limit*."""
    text = clean_line(value if isinstance(value, str) else str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_bytes(value: Any) -> str:
    """Render a byte count compactly, for example ``74.0 MiB``; anything that is not a count is ``?``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return "?"
    if value < 1024:
        return f"{int(value)} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB"):
        amount /= 1024
        if amount < 1024:
            return f"{amount:.1f} {unit}"
    return f"{amount / 1024:.1f} TiB"


def _count(value: Any) -> int | None:
    """Return *value* as a non-negative integer, or ``None`` when it is not one (an event field is untrusted)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def progress_line(task: Any, done: Any, total: Any, unit: Any) -> str:
    """Render one transfer as ``task  [#####-----]  done / total`` with percentage, in text that is safe to show."""
    show = format_bytes if unit == "B" else (lambda number: f"{number} {literal(unit, limit=12)}")
    done_n, total_n = _count(done), _count(total)
    label = literal(task, limit=80)
    if done_n is None:
        return f"{label}: progress unknown"
    if not total_n:
        return f"{label}: {show(done_n)} received"
    share = min(done_n / total_n, 1.0)
    filled = round(share * BAR_WIDTH)
    return f"{label}  [{'█' * filled}{'░' * (BAR_WIDTH - filled)}]  {show(done_n)} / {show(total_n)}  {share * 100:.0f}%"


_KEPT_STYLES = frozenset({"bold", "dim", "italic", "underline", "reverse"})


def monochrome(line: Text) -> Text:
    """Return *line* without any colour: only emphasis (bold, dim, italic, underline) is kept, so the words and marks carry every meaning."""
    kept = " ".join(word for word in str(line.style).split() if word in _KEPT_STYLES)
    return Text(line.plain, style=kept)


class EventRenderer:
    """Render event records, one batch after another, into styled lines.

    Parameters
    ----------
    colour : bool, default=True
        Whether lines may be coloured; ``False`` (``NO_COLOR`` or ``[ui] color = "never"``) keeps only bold and dim.

    Attributes
    ----------
    detail_omitted : int
        The number of detail lines that were counted but not shown.
    """

    def __init__(self, colour: bool = True) -> None:
        self._colour = colour
        self._progress: dict[str, tuple[Any, Any, Any, Any]] = {}
        self._links: dict[str, str] = {}
        self._detail_shown = 0
        self.detail_omitted = 0

    def feed(self, records: Iterable[Mapping[str, Any]]) -> list[Text]:
        """Return the lines that *records* add; a record of an unknown kind adds nothing (a newer gupkg may add kinds)."""
        lines: list[Text] = []
        for record in records:
            handler = getattr(self, f"_on_{record.get('event')}", None) if isinstance(record.get("event"), str) else None
            if handler is not None:
                lines.extend(handler(record))
        return lines if self._colour else [monochrome(line) for line in lines]

    def progress_text(self) -> str | None:
        """Return the live text of the transfer that runs now (the latest started one), or ``None``."""
        if not self._progress:
            return None
        task = next(reversed(self._progress))
        return progress_line(task, *self._progress[task][1:])

    def finish(self) -> list[Text]:
        """Return the closing note about omitted lines (empty when nothing was left out)."""
        if not self.detail_omitted:
            return []
        note = f"    … {self.detail_omitted} more detail lines are in the log, not shown here"
        self.detail_omitted = 0
        return [Text(note, style="dim")]

    # -- records -----------------------------------------------------------------

    def _on_run_start(self, record: Mapping[str, Any]) -> list[Text]:
        """Show which command runs, in which mode and whether it runs elevated."""
        elevated = "  ·  elevated" if record.get("elevated") is True else ""
        return [Text(f"gupkg {literal(record.get('command', '?'), limit=60)}  ·  {literal(record.get('mode', '?'), limit=20)}{elevated}", style="bold")]

    def _on_log_link(self, record: Mapping[str, Any]) -> list[Text]:
        """Remember a package's log path to show it under the package's header."""
        if isinstance(record.get("package"), str) and isinstance(record.get("log"), str):
            self._links[record["package"]] = record["log"]
        return []

    def _on_package_start(self, record: Mapping[str, Any]) -> list[Text]:
        """Show the package header, and its log path when the run linked one."""
        view = record.get("view") if isinstance(record.get("view"), Mapping) else {}
        name = view.get("name", "?")
        lines = [Text(f"Package {literal(name, limit=120)}", style="bold magenta")]
        log = self._links.get(name) if isinstance(name, str) else None
        if log is not None:
            lines.append(Text(f"    log: {literal(log)}", style="dim"))
        return lines

    def _on_phase_start(self, record: Mapping[str, Any]) -> list[Text]:
        """Show a bold cyan phase header that fills the line."""
        name = literal(record.get("phase", "?"), limit=60)
        return [Text(f"━━ {name} " + "━" * max(3, 60 - len(name)), style="bold cyan")]

    def _on_phase_end(self, record: Mapping[str, Any]) -> list[Text]:
        """Show how the phase ended: a check, a cross or a dash, with the word and the elapsed time."""
        status = record.get("status") if isinstance(record.get("status"), str) else "?"
        mark, style = _PHASE_END.get(status, ("–", "dim"))
        phase = literal(record.get("phase", "?"), limit=60)
        label = phase if status == "ok" else f"{phase} {literal(status, limit=20)}"
        elapsed = record.get("elapsed")
        seconds = f"  {elapsed:.1f}s" if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) else ""
        return [Text(f"  {mark} {label}{seconds}", style=style)]

    def _on_step(self, record: Mapping[str, Any]) -> list[Text]:
        """Show a detail line with its mark (a check, a plus or a minus), counting it instead when too many were shown."""
        if not self._room():
            return []
        mark, style = _STEP.get(record.get("kind"), ("", "dim"))
        parts = literal(record.get("text", ""), limit=MAX_LINE_CHARS * 5).splitlines() or [""]
        return [Text(f"    {mark if index == 0 else ''}{literal(part)}", style=style) for index, part in enumerate(parts[:50])]

    def _on_subprocess_line(self, record: Mapping[str, Any]) -> list[Text]:
        """Show one line of child-process output, dimmed, counting it instead when too many were shown."""
        if not self._room():
            return []
        return [Text(f"      │ {literal(record.get('line', ''))}", style="dim")]

    def _on_progress(self, record: Mapping[str, Any]) -> list[Text]:
        """Keep the live state of a transfer; a finished transfer leaves one lasting line."""
        task = literal(record.get("task", "?"), limit=80)
        done, total, unit = record.get("done"), record.get("total"), record.get("unit", "B")
        total_n, done_n = _count(total), _count(done)
        if total_n and done_n is not None and done_n >= total_n:
            self._progress.pop(task, None)
            return [Text(f"    ✓ {progress_line(task, done, total, unit)}", style="green")]
        self._progress.pop(task, None)
        self._progress[task] = (task, done, total, unit)
        return []

    def _on_prompt(self, record: Mapping[str, Any]) -> list[Text]:
        """Show a question the run was about to ask."""
        return [Text(f"? {literal(record.get('text', ''))}", style="bold")]

    def _on_warning(self, record: Mapping[str, Any]) -> list[Text]:
        """Show a warning; the word ``Warning:`` carries the meaning, yellow only reinforces it."""
        return [Text(f"Warning: {literal(record.get('text', ''))}", style="yellow")]

    def _on_error(self, record: Mapping[str, Any]) -> list[Text]:
        """Show an error and its hint."""
        lines = [Text(f"Error: {literal(record.get('text', ''))}", style="bold red")]
        if isinstance(record.get("hint"), str) and record["hint"]:
            lines.append(Text(f"  hint: {literal(record['hint'])}", style="dim"))
        return lines

    def _on_package_end(self, record: Mapping[str, Any]) -> list[Text]:
        """Show how the package ended, and drop any transfer that never completed."""
        self._progress.clear()
        result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
        ok = result.get("ok", True) is not False
        status = result.get("status") if isinstance(result.get("status"), str) else ("ok" if ok else "failed")
        return [Text(f"Package {literal(result.get('name', '?'), limit=120)}: {literal(status, limit=40)}", style="green" if ok else "red")]

    def _on_unreadable(self, record: Mapping[str, Any]) -> list[Text]:
        """Say that part of the events file could not be read (a skipped line is never silent)."""
        return [Text(f"Warning: the events file could not be read here ({literal(record.get('reason', 'unreadable'), limit=200)})", style="yellow")]

    def _room(self) -> bool:
        """Return whether one more detail line may be shown, otherwise count it as omitted."""
        if self._detail_shown >= MAX_DETAIL_LINES:
            self.detail_omitted += 1
            return False
        self._detail_shown += 1
        return True


# ---------------------------------------------------------------------------
# The ending of a log
# ---------------------------------------------------------------------------


def summarize_entries(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return one summary line per result entry (``ok name 1.2: updated (was 1.1)``), naming every failure and skipped target.

    Parameters
    ----------
    entries : sequence of mapping
        The per-target entries of result documents (``package``, ``ok``, ``status``, ``version``, ``previous``, ``error``).

    Returns
    -------
    list of str
        Text that is safe to show, followed by a count of the failed targets when there are any.
    """
    lines: list[str] = []
    failed: list[str] = []
    for entry in entries:
        name = literal(entry.get("package", "?"), limit=120)
        status = literal(entry.get("status", "?"), limit=60)
        error = entry.get("error") if isinstance(entry.get("error"), Mapping) else {}
        if entry.get("ok") is not True:
            failed.append(name)
            reason = literal(error.get("message", status), limit=500)
            lines.append(f"x {name}: {literal(entry.get('operation', 'operation'), limit=30)} failed: {reason}")
        elif status in ("skipped", "cancelled", "refused"):
            lines.append(f"- {name}: {status}" + (f" ({literal(error.get('message'), limit=300)})" if error.get("message") else ""))
        else:
            version = f" {literal(entry['version'], limit=60)}" if entry.get("version") else ""
            previous = entry.get("previous")
            was = f" (was {literal(previous, limit=60)})" if previous and previous != entry.get("version") else ""
            lines.append(f"ok {name}{version}: {status}{was}")
    if failed:
        lines.append(f"{len(failed)} of {len(entries)} failed: {', '.join(failed)}")
    return lines


def verdict_lines(exit_code: int, summary: Sequence[str], *, plain: bool = False, colour: bool = True) -> list[Text]:
    """Return the header and the indented summary that end a log: ``━━ Done`` or ``━━ Failed (exit code N)`` come last.

    Parameters
    ----------
    exit_code : int
        The combined exit code of the run; zero is success.
    summary : sequence of str
        The summary lines to show under the header (already safe text).
    plain : bool, default=False
        Use the ``==>`` header of redirected console output instead of the heavy rule.
    colour : bool, default=True
        ``False`` keeps only emphasis, no colour.
    """
    ok = exit_code == 0
    title = "Done" if ok else f"Failed (exit code {exit_code})"
    header = f"==> {title}" if plain else f"━━ {title} " + "━" * max(3, 60 - len(title))
    lines = [Text(header, style="bold green" if ok else "bold red"), *(Text(f"    {line}") for line in summary)]
    return lines if colour else [monochrome(line) for line in lines]
