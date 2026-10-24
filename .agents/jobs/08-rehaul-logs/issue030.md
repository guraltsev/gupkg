# Issue 030 worker log

Branch: rehaul/issue030 (from main 7120f63). Baseline on main: 3495 passed, 0 failed, 0 skipped (controller's notes).

## Plan

New packages beside `gupkg` and `gupkg_migrate` in `src/` (the bootstrap makes `src/` importable; D44: they read through core and act
through the `gupkg` CLI):

- `gupkg_ui/` (shared): `client.py` (the ONE module that starts the CLI: sibling `gupkg.cmd` only, argv lists, hostile characters refused,
  `"api": 1` checked, capped output), `follow.py` (bounded, strict follower of `--events-file` files: global file plus the package files
  named by `log_link`), `render.py` (event records to phase-styled Rich `Text`, never markup), `policy.py` (feeds `model/manager_policy.py`
  from plan and error documents), `session.py` (runs the steps of a batch, cancel through the stop marker, combined result),
  `settings.py` (reads `[manager]`, `[cleanup]` through core), `screens/` (frame, run view, package view, log view, modals).
- `gupkg_manager/`: `batch.py` (inventory, plan, confirm, grouped execution), `cli.py` (`check-updates --all`, `update --all`, `check`,
  `tui`), `app/` (the Textual app and the browser). `gupkg_tui/`: `cli.py`, `app.py` (one package).
- Launchers `src/gupkg/gupkg-tui.cmd`, `src/gupkg/gupkg-manager.cmd` (like `gupkg-migrate.cmd`), `bootstrap.py` programs `tui`/`manager`.
- `tools/build_standalone.py`: copy the three packages into the bundle, probe lists.
- Docs: `_docs/tui_style_guide.md` (rewritten), README, `src/gupkg/README.md`, `tests/manual_smoke.md`, issue/design text corrections.

## Acceptance criteria -> coverage

(filled in at the end)

## Unclear points / decisions

(filled in as they come up)
