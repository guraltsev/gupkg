# T04: Shared support code, one copy of each fixture

Issue 036 F8; `_docs/tests.md` §3, §4.3.

## Goal

The suite's shared code lives in `tests/support/`. Each fixture exists once. No test module imports from another. This
task changes no test's assertions.

## Change

1. **Move the helper modules** into `tests/support/` (`archive_support.py`, `catalog_support.py`, `cli_support.py`,
   `engine_support.py`, `http_fixtures.py`, `mode_support.py`, `scope_support.py`, `ui_support.py`) and update every
   import. Keep their names, without the `_support` suffix where it would be redundant (`tests/support/cli.py`). List the
   renames in your log.
2. **Remove `pin_no_ambient_elevation`** from `mode_support` and its imports (42 files). The autouse fixture
   `no_ambient_elevation_marker` in `tests/conftest.py` already does the same. Also remove the local copies
   `test_scope.no_ambient_elevation` and `test_prepare.not_marked_elevated`. Remove `World.mode_context()` if nothing
   calls it.
3. **One `world` fixture per kind of world**, in `tests/conftest.py` or `tests/support/`:
   - `world` (`World(tmp_path)`, the engine's fake machine) and `elevated_world`;
   - `cli_world` (`CliWorld(tmp_path)`).

   Delete each module's copy. Keep a module's own fixture only where it really differs (for example, the engine-check
   world with a published package), and name it after what it holds. Files that build `World(tmp_path)` inline in every
   test use the fixture instead.
4. **Remove imports between test modules.** Move what is shared into `tests/support/`. Known cases:
   - `test_engine_unowned_files` imports from `test_engine_name_conflicts`;
   - `test_scope_isolation` imports from `test_cli_tools` and `test_ui_screens`;
   - `test_ui_name_conflict` and `test_ui_unowned_file` import from another test module;
   - `test_manager_cli` imports helpers from `test_ui_flows`.
5. **Merge the copied helpers** into one each:
   - `registry_zip` (cli_reading and cli_tools);
   - `current()` (cli_lifecycle and cli_machine_interface);
   - `_Collector` (run_logs, event_contract and download_progress);
   - `Terminal` (manager_cli, duplicating `TerminalInput` in `cli_support`);
   - the hash-corruption snippet (4 copies);
   - `test_scope.Machine` (a near-copy of `mode_support.Machine`).

## Done when

- [ ] `git grep -n "pin_no_ambient_elevation"` prints nothing.
- [ ] H1 has no baseline line left (no test module imports another), and the H1 lines are removed from the baseline.
- [ ] `git grep -n "^def world(" tests` lists only the shared fixture and the justified variants, each named in your log.
- [ ] The collected tests are exactly those of `main` (same count; show it). No assertion changed:
      `git diff main -- tests | Select-String '^[-+]\s*assert'` shows only moved lines. Explain any exception.
- [ ] `scripts\test.cmd --all` passes.
