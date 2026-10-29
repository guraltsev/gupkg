"""Run one gupkg operation with its reporters installed and its log files managed.

A ``RunSession`` is what a front end wraps around an operation: it installs a
console (or UI) reporter together with the run's log writer, announces the run,
and at the end emits the closing summary, which always names the log paths and
is always the last event. A logging problem becomes one warning and never
changes the operation's result.

Usage and API
-------------
Use ``with RunSession(...) as run:``; call ``run.bind(root)`` when the root the
run acts on is known, and ``run.finish(summary, exit_code)`` with the result
text. A session left without ``finish`` ends as an internal error, so
the summary is never missing.

Implementation Approach
-----------------------
The session combines its reporters with ``MultiReporter`` and installs them
through the context variable of ``gupkg.report.events``. The log writer is
bound late, because the root is only known after the target has been resolved.
"""

from __future__ import annotations

import traceback as traceback_module
from pathlib import Path
from typing import Any, Sequence

from .events import (
    Diagnostic, MultiReporter, Reporter, RunEnd, RunStart, Warning, emit, use_reporter,
)
from .logfile import RunLog


class RunSession:
    """Install reporters and log files for one run and close it with a summary.

    Parameters
    ----------
    command : str
        Command name, for example ``install``.
    argv : sequence of str
        The command-line arguments, recorded in the log header.
    mode : str
        Install mode or scope in effect, recorded in the log header.
    elevated : bool
        Whether the run is elevated.
    version : str
        The gupkg version.
    reporters : sequence of Reporter
        Front-end reporters (console, TUI) that receive every event.
    events_name : str, optional
        Plain file name that also receives the event stream in each log directory.
    keep_runs, keep_days : int
        Log retention.
    fallback_base : Path, optional
        Folder whose ``.gupkg\\logs`` receives the run log when ``bind`` was never called.
    settings_layers : sequence of str
        Names of the settings layers in effect, recorded in the header.
    stop_file : str, optional
        The run's stop marker, announced in ``RunStart`` (decision D37).
    scope : str, optional
        The text ``<mode> root <path> (<source>)`` of the run's scope, recorded in the log headers.
    log : bool, default=True
        Set false to keep a run out of the log files (used where no log may be written).
    clock : callable, optional
        Returns the current UTC time for the log files; tests inject a fixed one.
    """

    def __init__(
        self,
        *,
        command: str,
        argv: Sequence[str],
        mode: str,
        elevated: bool,
        version: str,
        reporters: Sequence[Reporter],
        events_name: str | None = None,
        keep_runs: int = 50,
        keep_days: int = 90,
        fallback_base: Path | None = None,
        settings_layers: Sequence[str] = (),
        stop_file: str | None = None,
        scope: str = "",
        log: bool = True,
        clock=None,
    ) -> None:
        self._start = RunStart(command, tuple(argv), mode, elevated, version, tuple(settings_layers), stop_file=stop_file)
        self._fallback = fallback_base
        self._finished = False
        self._context: Any = None
        self._hub: MultiReporter
        self.log: RunLog | None = None

        # The log writer reports its own failures through the hub, so every
        # other reporter shows the single warning.
        all_reporters: list[Reporter] = list(reporters)
        if log:
            self.log = RunLog(
                command, lambda text: self._hub.handle(Warning(text)),
                events_name=events_name, keep_runs=keep_runs, keep_days=keep_days, scope=scope, clock=clock,
            )
            all_reporters.append(self.log)
        self._hub = MultiReporter(all_reporters)

    def __enter__(self) -> "RunSession":
        """Install the reporters and announce the run."""
        self._context = use_reporter(self._hub)
        self._context.__enter__()
        emit(self._start)
        return self

    def bind(self, base: Path | None, package_label: str | None = None) -> None:
        """Tell the log writer which root (or package folder) this run acts on."""
        if self.log is not None and base is not None:
            self.log.bind(Path(base), package_label)

    def discard_log(self) -> None:
        """Stop writing log files for this run, because it handed its work to another process that logs it itself.

        The run's own events still reach the other reporters; no log or events file is created afterwards.
        """
        if self.log is not None:
            self.log.close()

    def finish(self, summary: str, exit_code: int, result: dict[str, Any] | None = None) -> None:
        """Emit the closing summary, with the log paths appended and the result document attached; later calls are ignored."""
        if self._finished:
            return
        self._finished = True

        # A run that never named its root logs in the running gupkg's own place.
        if self.log is not None and self._fallback is not None:
            self.log.bind(self._fallback)
        lines = self.log.summary_lines() if self.log is not None else []
        text = "\n".join([part for part in [summary.rstrip("\n"), *lines] if part])
        emit(RunEnd(text, exit_code, result))

    def __exit__(self, exc_type, exc, traceback) -> None:
        """End the run (as an internal error if it was never finished) and uninstall the reporters."""
        try:
            if not self._finished:
                detail = f": {exc}" if exc is not None else ""
                if exc is not None:
                    emit(Diagnostic("".join(traceback_module.format_exception(exc_type, exc, traceback))))
                self.finish(f"gupkg stopped unexpectedly{detail}", 4)
        finally:
            if self.log is not None:
                self.log.close()
            self._context.__exit__(exc_type, exc, traceback)
