# T05: The `Installation` object; the side comes from the token

Issue 035: §4.1, §4.2, D47, D48. This is the central refactor. Go slowly, one caller at a time, and run the suite often.

## Goal

At process start the CLI builds one frozen `Installation(side, root, root_source, bin_dir, start_menu, env_target,
integrate, settings)`. Every subsystem receives it. Nothing selects a mode any more.

## Remove

- **The `--mode` option** of every command (`cli.py` `_options`, and the `mode=` arguments of the lifecycle entry points
  in `engine/lifecycle.py` and of `Request`).
- **The selector prefixes** `system:` and `user:` (`catalog.py` `_MODE_PREFIX`, `Selector.mode`, `cli.py`
  `_mode_prefix`, `_qualify`).
- **Mode selection and its leftovers:** `select_mode`, `ModeChoice`, `AUTO_ELEVATED`, `layers_of`, `default_mode`, and
  the adapter marked in T04.
- **From `modes.py`:** `resolve_mode`'s mode logic, `_container_mode`, `_shadow_warnings`, `settings_loader`.
- **The "roots differ" comparison** (`modes.py` ~238–244).

## Add

1. **`src/gupkg/model/installation.py`:** the frozen `Installation` dataclass. It replaces `ModeContext`: same helper
   methods (`package_dir`, `package_log_dir`, `package_data_dir`, `data_dir`, `log_dir`, `path_line`) and the fields
   above. `side` is `"system"` or `"user"`. Delete `ModeContext` and `model/mode.py` once nothing imports them (keep
   `ModeRefused`/`InputNeeded` by moving them into `installation.py`).
2. **`src/gupkg/installation.py`:**
   `build_installation(*, system, env, location, root_option, settings_path, read_text) -> Installation`:
   - `side = "system" if is_elevated_run(system) else "user"`;
   - an elevated run whose gupkg tree (`location`) is not admin-only raises `ModeRefused`: "an elevated gupkg must be
     installed in an admin-only root; run the system gupkg";
   - the root comes from T04's `resolve_root`, and the settings from T04's `load_side_settings`.
3. **CLI:**
   - `--root DIR` is a root option (beside `--settings` and `--sandbox`);
   - `_run` builds the installation once and passes it down;
   - the Resolve phase prints `<side> root <path> (<source>)`, for example `user root C:\Users\x\opt (built-in default)`;
   - the same text goes into the run log.
4. **`modes.py` keeps only:** resolving a target inside the installation's root, and the distribution copy-in
   (`_copy_source`). Rename what is left if that makes it clearer, and update imports.
5. **The side check of T02** (`check_supported`) now compares the package's `modes` with `installation.side`.

## Order that keeps the suite green

1. Add `Installation` and `build_installation`, with their tests.
2. Make `ModeContext` a thin alias of `Installation`, and switch callers one by one: engine, effects, planner, executor,
   health, checkupdates, cli commands, gupkg_ui. Run the suite after each module.
3. Remove `--mode`, the prefixes and `select_mode`, then delete the alias.

## New tests (each must fail on `main`, then pass)

- [ ] **Not elevated** (`FakeSystem`): `install pkg` installs into the user root. **Elevated:** into the system root.
      Neither run takes a flag.
- [ ] `--mode user` and `system:pkg` are usage errors (exit code 2).
- [ ] An elevated `FakeSystem` run whose gupkg location is user-writable fails at start with exit code 2. Nothing is read
      from any root (use a recording `read_text`).
- [ ] The Resolve line and the log name the side, the root and the source.

## Done when

- [ ] `git grep -n "select_mode\|ModeChoice\|ModeContext\|default_mode\|AUTO_ELEVATED\|layers_of\|\-\-mode\|_mode_prefix\|_shadow_warnings"`
      prints nothing, except in `_docs/` (T12 fixes docs).
- [ ] `src/gupkg/model/mode.py` is deleted. `src/gupkg/modes.py` is under 200 lines, or deleted.
- [ ] The full suite passes. The counts are in your log, with the number of deleted tests and why.

## Do not

- Change how the catalog scans roots. That is T06.
- Remove the relaunch. That is T07. Until then, a non-elevated run is the user side, so system targets simply do not
  exist there. Delete the tests that relied on planning system targets from a normal run, and list them in your log.
