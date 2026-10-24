"""Show a package's log file in a scrollable view, reading only the end of a large file.

The log is a plain text file that gupkg wrote; it is shown literally (escape sequences and control characters removed), never as markup, and only the last
``TAIL_BYTES`` of it are read so a huge log neither freezes the view nor fills memory. The path must lie inside the package's own ``.gupkg\\logs`` folder, so
a receipt that names some other file cannot make the view read it.

Usage and API
-------------
Push ``LogScreen(path, package_dir, title)``; Escape or ``q`` returns to the screen below.

Implementation Approach
-----------------------
The file is read in a worker thread (a blocking read never stalls the interface), cut into lines and written to an append-only log widget that keeps at most
``MAX_LINES`` lines.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import RichLog

from ..render import literal
from .frame import FrameScreen

# The most bytes read from the end of a log, and the most lines shown.
TAIL_BYTES = 2 << 20
MAX_LINES = 5000


def read_tail(path: Path, package_dir: Path | None) -> tuple[list[str], str | None]:
    """Return the last lines of the log *path* (cleaned) and a note, or no lines and the reason it cannot be shown.

    Parameters
    ----------
    path : Path
        The log file.
    package_dir : Path, optional
        The package whose ``.gupkg\\logs`` folder must contain *path*.
    """
    # The file must be a plain file inside the package's own log folder.
    try:
        resolved = Path(os.path.realpath(path))
        if package_dir is not None and not resolved.is_relative_to(Path(os.path.realpath(package_dir)) / ".gupkg" / "logs"):
            return [], f"{literal(path)} is not inside the package's log folder, so it is not shown"
        size = resolved.stat().st_size
        with open(resolved, "rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read(TAIL_BYTES)
    except OSError as error:
        return [], f"the log cannot be read: {literal(error)}"
    lines = data.decode("utf-8", errors="replace").splitlines()
    note = f"(only the last {TAIL_BYTES >> 20} MiB of {size >> 20 or 1} MiB is shown)" if size > TAIL_BYTES else None
    return [literal(line) for line in lines[-MAX_LINES:]], note


class LogScreen(FrameScreen):
    """A package's log, newest at the bottom.

    Parameters
    ----------
    path : str or None
        The log file; ``None`` when the package has no log yet.
    package_dir : Path or None
        The package whose log folder must contain the file.
    title : str
        What the log belongs to, for the status line.
    """

    BINDINGS = [Binding("escape", "back", "Back"), Binding("q", "back", "Back"), Binding("end", "bottom", "End")]

    def __init__(self, path: str | None, package_dir: Path | None, title: str) -> None:
        super().__init__()
        self._path = path
        self._package_dir = package_dir
        self._title = literal(title, limit=200)
        self._note = "loading"

    def key_hints(self) -> str:
        """Return the keys of the log view."""
        return "Esc back   ↑↓ PgUp PgDn Home End scroll"

    def status_text(self) -> str:
        """Return what is shown: the title and the path or a note."""
        return f"log of {self._title} · {literal(self._path or 'none yet', limit=300)} · {self._note}"

    def compose_body(self) -> ComposeResult:
        """Yield the log widget."""
        yield RichLog(max_lines=MAX_LINES, wrap=True, highlight=False, markup=False, auto_scroll=False)

    def on_mount(self) -> None:
        """Focus the log and load its end."""
        self.query_one(RichLog).focus()
        self.run_worker(self._load(), name="read log")

    async def _load(self) -> None:
        """Read the end of the log off the interface thread and show it."""
        log = self.query_one(RichLog)
        if not self._path:
            self._note = "no log has been written yet"
            log.write(Text("No log has been written for this package yet."))
        else:
            lines, note = await asyncio.to_thread(read_tail, Path(self._path), self._package_dir)
            self._note = note or f"{len(lines)} lines"
            for line in lines or ([note] if note else []):
                log.write(Text(line))
            log.scroll_end(animate=False)
        self.refresh_frame()

    def action_back(self) -> None:
        """Return to the screen below."""
        self.dismiss(None)

    def action_bottom(self) -> None:
        """Jump to the end of the log."""
        self.query_one(RichLog).scroll_end(animate=False)
