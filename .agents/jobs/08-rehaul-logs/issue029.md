# Issue 029 worker log

Branch: rehaul/issue029 (from main 4172a00). Baseline on main: 3301 passed, 0 failed, 0 skipped (controller's notes).

## What changed

New: `cli.py` (grammar, session, elevation, forwarders), `viewdata.py`, `settingsedit.py`, `authoring.py`, `report/results.py`,
`selfmgmt/location.py`, `selfmgmt/status.py`, `services/registrysource.py` (the eight transports moved from `registry.py`),
`gupkg-migrate.cmd` (+ `--gupkg-program=migrate` in `bootstrap.py`).
Engine/report/system additions: `Request.link_to/unlink` and `lifecycle.link`; exit code 6 (`requires_gupkg`, newer `format`, via
`model.manifest.NeedsNewerGupkg`); mixed-mode relaunch reports `not-run`; `RunStart.stop_file`, `RunEnd.result`,
`RunSession.discard_log`; `release_package_log` (a purge deleted the package folder while the run's own log was open: found by the
first real CLI run, invisible to engine tests that ran without a session); a duplicate final progress event removed;
git output streamed as events in unpack and track; `System.run_foreground` and `System.open_path`; `FakeHttp.report_size`.
Deleted: the 0.17 front end (module list in issue item 12), `core.py` trimmed to the error and atomic I/O, `modes.scope_context`,
`ModeContext.as_scope_paths`, `src/gupkg/gupkg-tui.cmd` (it called `gupkg tui`: infinite recursion with the forwarder).

## Decisions inside the issue's room

- `--stop`: no new option. The CLI names a unique marker per run (`<TEMP>\gupkg-stop-<uuid>`), announces it in `run_start`, removes it at the end.
- Elevated child: command line rebuilt from the parsed options; `--relaunched` always; `--yes` only if the user gave it, the parent's own
  answer travels as `--batch-confirmed`; `--settings`/`--sandbox` refused in the child; targets that name their mode are split.
- Forwarders refuse an elevated shell and root options; launchers are looked up in the gupkg tree only (never PATH).
- `list --installed` = has an active version (outdated and broken included); `--names` qualifies a name that exists in two modes.
- `pkg pin` on a verified install: nothing to pin (exit 0), no "unverified" warning.
- `allow_unc_file_urls`: not a setting (design 13.4 already says no setting enables network `file:` URLs).
- 026 handoff, confirmed: `check_updates_one` calls `require_trusted_tools(engine, *tracker_tools())` (interpreter and regex worker)
  before the tracker runs, patterns run in the subprocess regex worker (`System.regex_worker`).
- Tests that depended on 0.17 behaviour with no 1.0 equivalent were deleted (list in the report); 0.17 installs used by the migration
  tool's tests are now RECORDED from the real 0.17 code (`tests/fixtures/legacy_017`, recorder `record.py`).

## Unclear points / open

- The parent of an elevated relaunch does not wait for the child (`ShellExecute runas`); its exit code is 0 with `relaunched: true`.
- `pkg bundle` writes no launchers (issue 033's template); `[ui] color` is not read.
