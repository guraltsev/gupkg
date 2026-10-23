# Issue 025 worker log

Branch: rehaul/issue025 (from main 86d352a). Baseline: 821 passed, 0 failed, 0 skipped (main checkout's `.venv` python
run from the worktree root). After: 944 passed, 0 failed, 0 skipped.

## Files

New: `src/gupkg/model/mode.py` (pure: `ModeContext`, `ModeChoice`, `select_mode`, `check_supported`, `build_mode_context`,
`ModeRefused` exit 2, `InputNeeded` exit 5), `src/gupkg/modes.py` (`resolve_mode`, `settings_loader`, `scope_context`,
`default_folders`), `src/gupkg/relaunch.py` (`ensure_elevation`), `src/gupkg/distcopy.py` (`copy_distribution`).
Tests: `test_mode_selection.py`, `test_mode_context.py`, `test_mode_resolve.py`, `test_mode_elevation.py`,
`test_distribution_copy.py`, helper `tests/mode_support.py`.
Changed: `model/settings.py` (`integration.user.start_menu`; `LAYER_PORTABLE`), `settingsload.py` (`portable_root` layer),
the 0.17 call sites of the deleted sources (`layout.compute_scope_paths`, `manager.InstallationContext`,
`distribution._user_bin/_system_bin`: gone; `windows.environment_registry_location` and the second admin check were already
removed by 021), design 6/11/12 text, `_docs/operations.md` (why no portable in the system root), `tests/manual_smoke.md`.

## Readings of unclear text (decisions made)

- **"Configured portable root" (D42) means a settings layer other than the built-in defaults set `[roots].portable`.**
  The built-in default `%USERPROFILE%\opt-portable` still names the root that listings scan, but `--mode portable` on a
  name asks when only the default exists (D42 rejected "a fixed default"). Open question for the controller (non-blocking).
- **Elevated plus portable is refused like user** (D31/9.6: a portable root is user-writable; design 6.3 "never machine-wide").
- **A portable root may not be the system root, inside it, or contain it; nor equal/nest with the user root** (one folder,
  two modes). The issue states only the first part.
- **An explicit mode that contradicts the install record or containing root is refused**, and so are a record and root that
  disagree (the problem text: "scope is not tied to the package"). 6.2 says the flag wins; it does for what it can decide
  (a package not installed yet, a folder outside every root).
- **A record that an elevated run read from a folder that is not admin-only is refused** (not ignored): fail closed.
- **Distribution copy-in** (design 21.4) is owned by 028/033 as engine behaviour; 025's T1 bullet needs its admission rule
  and a safe copy primitive, so `resolve_mode` returns `source` (the folder to copy) and `copy_distribution` copies it
  (no links followed, data stream only so no `Zone.Identifier`, never overwrites, `identical`/`diverged`) and reports
  `source_marked`. The receipt that records the mark is written by the engine (028); the field to record is
  `CopyResult.source_marked`.
- **Relaunched child** is a flag of the request (`ModeRequest.relaunched`); the CLI (029) decides how the child is told.
  A direct administrator shell (not relaunched) may name a folder outside every root, only to copy it in.
- **Portable persistence, `[nonportable]`, markers, `integrate = "none"` shims, `uninstall --purge`**: the issue itself
  defers the end-to-end checks to 028. 025 provides the data they read (`ModeContext.package_data_dir`, `bin_dir` =
  `<root>\bin`, `integrate`, `env_target`, `start_menu`, `path_line()`).
- **0.17 stays** (D13): the old `--scope`/manager commands keep their own `AUTO` meaning in `install_package` until 029;
  they take their destinations from `scope_context` now (`ModeContext.as_scope_paths`). `System.scope_folders` stays as the
  machine's default folders (so `--sandbox` stays off the real machine); a configured `[integration.*]` value wins over it.
- **`integration.user.start_menu`** added to the schema (design 6.1 says the Start Menu becomes configurable; schema had only
  `integration.system.start_menu`). Design 11 sample updated.
- Portable mode reads the portable root's settings layer instead of the user layer (6.1 "Settings layers read").
- 8.3 names: tested (`windows_integration`); `GetShortPathNameW` may equal the long name on a volume without short names.

## Acceptance criteria -> coverage

See the final report.
