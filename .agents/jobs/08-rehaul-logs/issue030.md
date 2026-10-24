# Issue 030 worker log

Branch: rehaul/issue030 (from main 7120f63). Baseline on main: 3495 passed, 0 failed, 0 skipped (controller's notes).

## What was built

New packages beside `gupkg` and `gupkg_migrate` in `src/` (the bootstrap makes `src/` importable; D44: they read through core and act through the
`gupkg` CLI):

- `gupkg_ui/` (shared by both programs): `client.py` (the ONE module that starts a program: sibling `gupkg.cmd` resolved inside the tree, argument
  lists, hostile characters refused, `"api": 1` checked, output capped, exit code and `run_end` cross-checked, `StopRequest`), `follow.py` (bounded, strict
  follower of `--events-file`: global file plus the package files named by `log_link`), `render.py` (records to Rich `Text`, literal and cleaned, bounded),
  `plans.py` (list/info/plan readers; `decide_targets` feeds `model/manager_policy.py`), `steps.py` (grouping: one non-elevated call, ONE call for all system
  targets), `session.py` (runs the calls, combines results, cancel), `flows.py` (plan, one confirmation, policy, grouped run, cleanup, check-updates),
  `settings.py`, `startup.py`, `console.py`, `interaction.py`, `app.py`, `screens/` (frame, run view, package view, log view, dialogs, version picker).
- `gupkg_manager/`: `batch.py` (inventory, selection, batch commands), `cli.py`, `app/` (application, browser). `gupkg_tui/`: `cli.py`, `app.py`.
- Launchers `src/gupkg/gupkg-tui.cmd`, `src/gupkg/gupkg-manager.cmd`; `bootstrap.py --gupkg-program=tui|manager`; `tools/build_standalone.py` copies the three
  packages and probes them.
- `_docs/tui_style_guide.md` rewritten; README, `src/gupkg/README.md`, `tests/manual_smoke.md`, design section 12 and the issue text updated.

## Deviations from the issue's file list (decisions inside its room)

- `client.py` and `policy.py` are shared, so they live in `gupkg_ui` (policy feeding is `gupkg_ui/plans.py`), not in each program; `gupkg_manager` has
  `batch.py`, `cli.py`, `app/`; `gupkg_tui` has `cli.py`, `app.py`. The one-package flows are the same code as the batch (a batch of one).
- The checksum questions are asked AFTER the one plan confirmation (a no costs nothing); the plan marks the packages that will be asked.
- `--skip-checksum` is per call: the call of a group carries it when any target there needs it (a declared hash is always verified); still one call per group.
- A system target's `[manager] checksum_missing` is judged as an elevated run judges it, and also distrusts `--settings` (an elevated child ignores it).
- `check-updates` is ONE non-elevated call (the engine never relaunches for it, D22): see "Open questions".
- `--yes` is passed to the CLI after the UI confirmed (the CLI always runs `--no-input`); `check-updates --all` passes `--yes` itself (it activates nothing).
- `update --all` selects `outdated` packages; `check-updates --all` selects installed, outdated, local and broken ones; `check` every package in a root.
- Events directory of a call = `<first target's package parent>\.gupkg\logs` (from the package path in `list`), so no settings lookup is needed.
- The programs accept `--settings`/`--sandbox` and relay them (needed to run the real CLI against temp roots); the forwarders still refuse them.

## Acceptance criteria -> coverage

See the final report (table) and `tests/test_ui_*.py`, `tests/test_manager_cli.py`, `tests/test_manager_real_cli.py`.

## Findings on the way

- A real elevated sandbox (`system.json` with `elevated`) reads the REAL `C:\opt` because an elevated run ignores `--settings` outside the (default) system
  root: tests never use an elevated sandbox. The system-target rows are covered with the scripted double.
- Pilot tests found: a worker that finishes after its screen was left crashed the app (`NoMatches`); fixed (`exit_on_error=False`, guarded updates).
- Rich on a redirected cp1252 stdout crashed on the heavy rule characters: the programs reconfigure redirected streams to UTF-8 (like the CLI).
