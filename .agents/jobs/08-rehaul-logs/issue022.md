# Issue 022 worker log

Branch: rehaul/issue022 (from 498c491). Baseline on main: 419 passed, 0 failed, 0 skipped.
After: 488 passed, 0 failed, 0 skipped (the `windows_integration` test of real child output included).

## Files changed

- New `src/gupkg/report/`: `events.py` (event dataclasses, `Reporter`, `MultiReporter`,
  contextvar `use_reporter`/`emit`, `phase`, `package_scope`, `subprocess_lines`,
  `event_to_dict`), `console.py` (Rich renderer, `PlainFormatter`, `LineReporter`),
  `logfile.py` (`RunLog`, `validate_log_file_name`, `running_gupkg_logs_base`, retention),
  `session.py` (`RunSession`).
- Producers: `core.py` (`log_*` become adapters; `colorize`, `log_progress`, the stdout
  logger are gone), `downloads.py` (`Progress` events), `gupkg.py` (Resolve, Activate, Fetch,
  Check phases), `components.py` (one phase per component), `origin.py`, `updates.py`,
  `registry.py` (git through `on_line`, no stderr-to-warning), `distribution.py` (Check runtime).
- Front ends: `cli.py` (`RunSession`, `--events-file`, summary last, human output to stderr
  with `--format toml`), `commands.py` (no `output` redirection, `LineStream` removed,
  `package_scope` per package, `emit_problems`), `outcome.py` (`format_human` returns text
  only), `manager.py` (`_quiet` removed; `_scoped` per target, `ManagedTarget.scope_root`),
  `tui.py` (`LineReporter` + session), `manager_tui.py` (log-only sessions).
- `system/windows.py`: `run` reads child output as bytes and decodes UTF-8 first (then the
  code page), splitting lines at LF, CRLF and lone CR as they arrive.
- Tests: new `test_event_contract.py`, `test_console_renderer.py`, `test_run_logs.py`;
  rewritten `test_download_progress.py`, `test_color_output.py` (kept the TUI part),
  three TUI tests in `test_tui.py` (+1 log test); `test_line_stream.py` deleted;
  `test_release_bundle.py` (stdout instead of stderr), `test_no_runtime_acquisition.py`
  (works on a copy of the fixture: a run now writes logs beside its package).
- Docs: `README.md` (`--events-file`, "Output and logs"), `src/gupkg/README.md`,
  `tests/manual_smoke.md` (section for 022).

## Acceptance criteria and coverage

See the final report.

## Decisions taken inside the issue's room

- Root of a single-package run is the package folder's parent; a manager run uses the
  configured root of its scope (`ManagedTarget.scope_root`).
- The run log's name carries the package when the run acts on exactly one package
  (`...-install-firefox.log`); package logs are `...-install.log`.
- Links in the run log: text line `Package <name>: <path>` and a JSONL record
  `{"event": "log_link", package, log, events, events_file, ts}` written by the log writer
  (not a producer event). `RunStart` gained `settings_layers` (default empty) and `api`.
- `--events-file` name: ASCII letters, digits and `._+-`, starts with a letter or digit,
  at most 100 characters, no device names, no trailing dot, not `*.log`. The directory's
  real path must stay inside its package or root, and the file must not be a link or junction.
- Warnings and errors are de-duplicated by `emit` (per package, and at run level against
  every package's), so a problem reported live and again in a result appears once.
- A failing reporter is dropped by `MultiReporter` and the others get one `Warning`; the
  log writer switches a failing location off and warns once.
- Final progress event of a download has `done == total` (the size is known by then).

## Unclear points

- Issue says `downloads.download_response`; the function is `download_file` (text fixed).
- "Update session" has no phase of its own in this code base; the update flow emits `Check`
  and `Fetch`, and activation reuses `install_package`'s phases.
- `[logs] keep_runs` / `keep_days` belong to issue 023's settings; the writer takes them as
  parameters (defaults 50 and 90) and the CLI uses the defaults.
- The manager TUI still uses its own `on_progress` callbacks (target states); replacing them
  is the manager rewrite, not this issue.

## Review round 1

- Merged main (issue 023): conflicts in `components.py`, `core.py`, `manager.py` resolved by keeping both
  sides; `import json` in `manager.py` was unused after the merge and is dropped with `contextlib`/`io`.
- Events file: stale plain file unlinked, then created exclusively (`"x"`); a link, directory or file with
  more than one name under the name is refused; after creation `fstat` must show one name and a regular
  file. The realpath-parent and `samestat` checks of the first draft were dropped (redundant).
- `latest.log` is built in a unique `latest.<uuid>.tmp` created exclusively; names `latest.*` and run-file
  names are refused. Pruning touches regular files only (tested with a symlink).
- `_output_lines`: scan offset (each read scanned once) and 64 KiB line cap (`[line continues]` note on the
  reported piece, never inside a UTF-8 character); direct tests in `test_process_output_lines.py`.
- Console: `ConsoleReporter(problems_to_stderr=True)` sends Warning and Error to stderr (0.17 behaviour),
  `--format toml` sends everything to stderr; Step, Warning, Error, Prompt and summary text are cleaned on
  the console and in the text log (JSONL verbatim, subprocess lines cleaned at capture).
- Documentation of the points above is in README ("Output and logs") and the `events.py` docstrings.
