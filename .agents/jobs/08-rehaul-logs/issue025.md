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

## Review round 1

- `copy_distribution` is hardened against a source that changes while it is read: a directory is listed only after it is
  shown to be a plain directory inside the source and the listing counts only if `lstat` is unchanged afterwards; a file is
  opened first and the handle must be the file the path names (`fstat` vs `lstat` identity, regular file, not a link) and
  the path's real path must still lie inside the source; the destination is re-checked to be inside the staging folder.
  Tests swap a directory, and a parent folder, for a junction between check and use (by wrapping `os.scandir` / `os.open`).
  Weaker than asked: the final path is checked through the real path of the *path* after `open`, not through
  `GetFinalPathNameByHandle` (that needs ctypes outside `system/windows.py`, which the 021 acceptance rule forbids, or a new
  `System` method). A swap back between the open and the real-path check is therefore not excluded; the sources of
  the copy are data and are never executed.
- Hostile names (trailing dot or space, reserved device names with or without extension, superscript digits, stream
  syntax, illegal characters) are `DistributionRefused` up front, for the package and version names and for every entry.
  `DistributionRefused` subclasses `ModeRefused` (exit code 2).
- A refused or failed copy removes the work folder and any of root, `.gupkg`, `.gupkg\work` that the call created.
- `copy_distribution(required=ModeChoice)` checks the staged `pkg.toml`'s `modes` before anything is moved into the root
  (same `ModeRefused` text as `check_supported`).
- Portable root rule (controller decision): refused when it is the system root, inside it, or contains it; refused when it
  is the user root or inside it; allowed when it only contains the user root. The message names the rule that fired.
- The `GUPKG_ELEVATED` pin is an autouse fixture in `tests/mode_support.py`, imported by every mode test module.
- `ModeContext.package_dir` validates the name (no `..`, empty, separators, colon, trailing dot).
- Design: portable refused under elevation (6.2, 9.4, D31); the default portable root is a scanned location, not a `--mode
  portable` target (6.1).
- `scope_context(system=None)` (used by `self status` without a system) now raises `SettingsError` (a `ValueError`) when
  `USERPROFILE` or another `%VAR%` of the built-in defaults is undefined; the old `_user_bin` fell back to the home folder.

### Handoff to 028 and 029

- Nothing is wired to a command yet: `resolve_mode`, `ensure_elevation` and `copy_distribution` are called by no CLI path.
- 029 must always set `ModeRequest.relaunched` in the argv of the elevated child (the child is stricter only if told)
  and test that the relaunched child refuses a folder outside every root.
- 028 writes `CopyResult.source_marked` into the receipt, passes `required=ModeChoice` to `copy_distribution`, and installs from
  the copy. `resolve_mode` returns the folder to copy as `ResolvedMode.source`.
