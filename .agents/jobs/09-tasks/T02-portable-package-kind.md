# T02: Portable is a package kind; packages declare their modes

Issue 035: §3.2, §4.6, §4.7, D51, D55. Owner answers Q5 (keep `modes`) and Q6 (remove unused portable features).

## Goal

After this task:

- **`modes` has one of three meanings:**
  - `["system", "user"]` (the default), `["system"]` or `["user"]`: the modes the package may be installed in;
  - `["portable"]`: the package is portable, and user only;
  - any list that mixes `portable` with `system` or `user` is invalid.
- **A portable package installs into the user root,** like any user package. There is no portable root and no
  `--mode portable`.
- **Per-mode steps are gone,** and so is the "App prepared for another mode" guard.

`--mode system|user` still exists after this task. T05 removes it. Do not touch it here.

## Read

- `src/gupkg/model/manifest.py`: `MODES`, `Portable`, `ModeSteps`, `_parse_portable`, `_parse_mode_steps`, `mode_steps`,
  `_check_variables` (`$Data`), the writer around line 1625.
- `src/gupkg/engine/portable.py`, `src/gupkg/engine/appbuild.py` (lines ~165–190),
  `src/gupkg/engine/lifecycle.py` (`_guard_the_app`, line ~640, and its caller ~549).
- `src/gupkg/model/mode.py`, `src/gupkg/modes.py`: everything about `MODE_PORTABLE`, `_portable_root`,
  `_check_portable_root`, `_in_place_shape`, `in_place`.
- `src/gupkg/settingsload.py`, `src/gupkg/model/settings.py`: `LAYER_PORTABLE`, `roots.portable`,
  `integration.portable`.
- `src/gupkg/engine/effects.py`, `executor.py`, `planner.py`, `health.py`, `catalog/catalog.py`, `cli.py` (`shell`,
  `_sandbox_install`, `_mode_of_root`): grep each for `portable`.

## Steps

1. **Manifest** (`model/manifest.py`):
   - validate `modes` as described in "Goal". The error text for a mixed list:
     `modes: a package is portable or not; publish a separate "<name>-portable" package`;
   - `[portable]` is allowed only when `modes == ["portable"]`, and is required then. Keep `persist` and `markers`.
     Remove `args`, `env` and `prepare` from `[portable]` (Q6);
   - remove the `[user]` and `[system]` tables (`ModeSteps`, `_parse_mode_steps`, `mode_steps`, the fields `user` and
     `system` of `Manifest`, and their writer lines). They become unknown keys;
   - remove the `$Data` variable;
   - remove `"user"` and `"system"` (as tables) and the `[portable]` keys you removed from the list of known keys near
     line 167.
2. **Engine:**
   - `engine/portable.py`: keep `hand_over`, `hand_back`, `_merge_newer` and `_confined`. Keep marker creation. Remove
     `mode_signature`. `apply_mode_adjustments` becomes `create_markers(app, manifest)`, which writes the markers only
     when the package is portable;
   - `engine/appbuild.py`: call `create_markers`. Stop writing `[app] prepared_for` to the receipt;
   - `engine/lifecycle.py`: delete `_guard_the_app` and its call. Delete the receipt reads of `prepared_for`;
   - the persist hand-over (`hand_over`/`hand_back`) runs when the **package is portable** (its manifest has
     `[portable]`), not when the "mode is portable". Grep `executor.py` and `lifecycle.py` for the condition and change
     it.
3. **Mode model** (`model/mode.py`, `modes.py`):
   - `MODES` for selection becomes `("system", "user")`;
   - delete `MODE_PORTABLE`, `_portable_root`, `_check_portable_root`, `_in_place_shape`, `ModeContext.in_place` and
     the `ask_portable_root` field;
   - the support check refuses a portable package in system mode with:
     `This is a portable package: it installs only for a single user. Run gupkg without administrator rights.`
   - the mode check (D55): a package whose `modes` lacks the chosen mode is refused **before any work** with:
     - `This package installs only system-wide. Run gupkg as administrator: use the Start Menu entry "gupkg manager
       (administrator)", or run in an administrator shell: gupkg <command> <name>` (Q12), or
     - `This package installs only for a single user. Run gupkg without administrator rights.`

     These go through the existing `check_supported`. Keep its call sites. `uninstall`, `check` and `cleanup` are
     never refused by it; add a test that uninstalls a package whose `modes` exclude the run's mode.
4. **Settings:**
   - remove `LAYER_PORTABLE`, `[roots].portable` and `[integration.portable]` from `model/settings.py`, `settingsload.py`
     (the `portable_root` parameter) and `_check_roots`;
   - `integrate = "none"` moves to `[integration.user] integrate = "full" | "none"` for now. T04 makes the table single.
     `ModeContext` for `user` honours it: shims go to `<root>\bin`, with no Start Menu and no env;
   - `gupkg shell` prints the PATH line when `integrate = "none"`.
5. **CLI:** remove `portable` from `--mode` choices, `--in-place` for portable, `_mode_prefix`/selector prefix
   `portable:`, the portable branch of `_context`, and the portable roots of `_sandbox_install`.
6. **Fixtures:** `tests/fixtures/registry_100` has 26 `pkg.toml` files mentioning portable and 3 with `[user]`/`[system]`.
   Convert them by the rules of issue 035 §4.6 "Registry", **in the fixture only**:
   - each tri-mode package keeps its name as non-portable (`modes = ["system", "user"]`, the `[user]` steps moved to
     `[[prepare]]`, no `[portable]`);
   - add a `<name>-portable` copy only where a test needs one;
   - if a fixture carries a fingerprint or hash that depends on the file bytes, update it the way
     `tests/test_registry_100.py` documents (read that test first). If it does not say how, stop and ask.
7. **Tests:**
   - delete the tests of portable roots, in-place portable, `$Data`, per-mode steps, `prepared_for` and the guard
     (mainly `tests/test_engine_modes.py`, `tests/test_mode_resolve.py`, `tests/test_mode_context.py`,
     `tests/test_mode_selection.py`, `tests/test_manifest_format2.py`, `tests/test_settings_layers.py`);
   - keep and adapt the tests of persist hand-over and markers so they use a portable package in the user root.

## New tests (each must fail on `main`, then pass)

- [ ] `modes = ["system", "user", "portable"]` is invalid, with the message above.
- [ ] `[user] prepare = ...` is an unknown key.
- [ ] A portable package (`modes = ["portable"]`, `[portable] persist = ["config"]`) installs in user mode into the user
      root. `data\config` survives an update from 1.0 to 1.1 (FakeSystem, `tmp_path`).
- [ ] The same package in system mode is refused before any file is created. Assert that the package folder, the bin
      folder and the receipt do not exist.
- [ ] `modes = ["system"]` in user mode is refused before any file is created. `modes = ["user"]` in system mode is
      refused the same way. Check both messages.
- [ ] `[integration.user] integrate = "none"`: shims go to `<root>\bin`, no env and no shortcut are written, and
      `gupkg shell` prints the PATH line.

## Done when

- [ ] `git grep -n "MODE_PORTABLE\|LAYER_PORTABLE\|roots.portable\|integration.portable\|prepared_for\|mode_signature\|_guard_the_app\|ModeSteps\|\$Data\|ask_portable_root"`
      prints nothing.
- [ ] `git grep -n "portable" -- src/gupkg` matches only: the package kind in `manifest.py`, `engine/portable.py`, the
      hand-over call sites, and the refusal message. List each match in your log.
- [ ] The full suite passes.

## Do not

- Remove `--mode system|user`, `default_mode`, the relaunch or the catalog's roots. Those are later tasks.
- Touch the `gupkg-registry` folder. That is T03.
