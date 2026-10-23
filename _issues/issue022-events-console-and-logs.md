# Issue 022: Event stream, console renderer, and standard logs

Date: 2026-10-22
Priority: High
Epic: 020. Design: `_docs/rehaul_design.md` §14. Supersedes the output parts of issue 015.

## Problem

- **Output is scraped text, not a model.**
  - Workflows print through `core.log_*` to a process-global
    `redirect_stdout` (`commands.run_guarded`).
  - `LineStream` splits on `\n` and `\r`.
  - The manager uses its own `on_progress` callbacks, so there are three
    output mechanisms.
- **Phases are indistinguishable from detail lines.** Phase lines are plain
  text ("Installing components...", "Creating shortcuts...").
- **Output is lost in several places.**
  - The manager's `_quiet` (`manager.py:838`) discards each package's output.
  - `git` and `pip` subprocesses write straight over the terminal, or over
    the TUI.
  - Origin script output is buffered and every stderr line becomes `WARNING:`.
- **Warnings are printed twice**: live, and again by `format_human`.
- **gupkg writes no log file.** After a TUI session, nothing remains.

## Scope

1. **`report/events.py`.** Event dataclasses as listed in §14.1, plus:
   - a `Reporter` protocol;
   - a `contextvars.ContextVar` holding the active reporter (or a `MultiReporter`);
   - `emit()`.

   For the transition, `core.log_info`, `log_warning` and `log_error` become
   adapters that emit `Step` and `Warning` events.
2. **Producers emit phase and progress events.**
   - `install_package`, `components.install_components` (its `run(title, step)`
     helper becomes `PhaseStart`/`PhaseEnd`), the update session, registry
     sync and self repair emit `PhaseStart` and `PhaseEnd`.
   - `downloads.download_file` emits `Progress(done, total)` events
     instead of formatted text.
3. **Subprocess output is streamed.** All subprocesses run through
   `System.run(..., on_line=...)` (issue 021) and emit `SubprocessLine`.
4. **`report/console.py`, a Rich renderer.**
   - Coloured, bold phase rules with a ✓ or ✗ and the elapsed time.
   - Indented, dimmed steps.
   - A transient `rich.progress` bar for downloads.
   - Yellow warnings and red errors, each printed once.
   - A final summary that names the log path.
   - **Fallback to plain text** when stdout is not a terminal or `NO_COLOR`
     is set: `==> Phase` headers, no carriage returns, and only the final
     progress line.
   - Human output goes to stderr whenever `--json` (issue 029) is active.
5. **`report/logfile.py`.**
   - **Locations (decision D26):**
     - **Per package:** everything about one package goes to that
       package's `<root>\<name>\.gupkg\logs\`.
     - **Per root:** `<root>\.gupkg\logs\`, in the root data directory
       (design §9.7), holds only global run information, with links to the
       per-package logs the run wrote. That is `C:\opt\.gupkg\logs\` for
       system runs and `%USERPROFILE%\opt\.gupkg\logs\` for user runs.
     - Both inherit the root's permissions, so system logs are admin-only.
     - **A bundled gupkg** logs a run that involves no package into its own
       package's `.gupkg\logs\`.
     - gupkg never logs to AppData or ProgramData (decision D20).
   - **Files per run:** `<UTC yyyymmddThhmmssZ>-<command>[-<package>].log`
     (plain text, flushed per line) and a `.jsonl` file of the events.
   - **`--events-file NAME`** (the flag is issue 029's) takes a plain file
     name, not a path. The CLI writes the event stream under that name into
     the run's log directories, and the caller reads it there (decision
     D26). This also keeps an elevated child from writing to a path that a
     standard user chose.
   - **Header and footer:** the header records the gupkg version, argv, mode,
     elevation and settings layers. The footer records the exit code.
   - **`latest.log`** is a copy of the most recent run's text log.
   - **Retention:** governed by `[logs] keep_runs` and `keep_days`, with
     pruning at run start.
   - **Never fatal:** a logging failure produces one warning and the operation
     continues.
6. **Batch output is logged.** The manager's batch execution logs each
   package's events into that package's log, links each one from the run
   log, and drops `_quiet`.

## Acceptance criteria

- [ ] `gupkg install <pkg>` on a console shows coloured phase rules, a live
      download bar and a final summary; the summary is the last thing
      printed.
- [ ] With output redirected to a file, the output has no ANSI codes and no
      `\r`, shows one final progress line per download, and has `==>` phase
      headers.
- [ ] Every run that acts on a package leaves a `.log` and a `.jsonl` in
      that package's `<root>\<name>\.gupkg\logs\`, and a run log in
      `<root>\.gupkg\logs\` that holds no per-package output and links to
      the package logs. Nothing is written under AppData or ProgramData. The
      final summary contains the log paths.
- [ ] A bundled gupkg run that involves no package leaves its log in its
      own package's `.gupkg\logs\`.
- [ ] `--events-file events.jsonl` makes the event stream appear in the
      run's log directories under that name. A value that contains a path
      separator is refused, and nothing is written outside the log
      directories.
- [ ] A failing `git clone` shows git's error lines inside the Fetch phase of
      both the console and the log. Nothing is written behind the TUI.
- [ ] Each warning appears exactly once in the console output and once in
      the log.
- [ ] A CLI call with several targets records each package's phase output
      in that package's log, and the run log links to each of them.

## Tests

- Renderer tests feed an event sequence and assert on the visible text in
  three cases: a terminal (Rich `Console(record=True)`), redirected output,
  and `NO_COLOR`.
- Log writer tests cover naming, flushing, retention and a non-writable
  directory, which must produce a warning and no failure.
- Delete `tests/test_line_stream.py` with `LineStream`; replace the TUI
  string assertions in `tests/test_tui.py:146-223` with renderer tests on
  visible text.
