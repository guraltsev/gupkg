# T07: Remove the elevated relaunch; machine interface `"api": 2`

Issue 035: §3.1 item 7, §4.4, D49.

## Goal

gupkg contains no code that starts an elevated process. A run is the side of its own token. The T1 checks inside the
system side stay, routed through one method.

## Remove

- `src/gupkg/relaunch.py` (the whole file).
- **`System.relaunch_elevated`** in `system/port.py`, `system/windows.py` (and `_shell_execute`, `_ShellExecuteInfo` if
  nothing else uses them) and `system/fake.py`. Also `ElevatedRun`, and `clamp_child_exit_code` if it is only used for
  the child.
- **`cli.py`:**
  - `--relaunched`, `--batch-confirmed`, `--stop-file`, and the `relaunch=` parameter of `_options`;
  - `_groups`, `_modes_of`, `_child_argv`, `_child_run_end`, `_missing_run_end`, `_relaunch_note` and `_argv_text` (if
    only the child uses it);
  - the relaunch handling in `_execute` and the child-only checks at the top of `_run`;
  - the elevation confirmation question.
- **`engine/lifecycle.py`:** the `ensure_elevation` block, the `mixed-modes` results, and `_needs_token`.
- **`engine/context.py`:** `relaunched`, `relaunch_argv`, `relaunch_note`, `request_stop` (only if it exists only for
  the child). Keep the stop marker the CLI uses for itself.
- **Result documents** (`report/results.py`): `relaunched`, `child_exit_code`, `child_raw_exit_code` and the
  `child-no-result` error. Set `API_VERSION = 2` in `report/events.py`. Update the contract tests, and
  `gupkg_ui/client.py`'s API check.
- **`unsafe_relaunch_argument`:** keep it only where a `.cmd` launcher receives an argument (`gupkg_ui/client.py`
  `refuse_unsafe`). Remove it from the CLI's own parsing.
- **Tests:** `tests/test_cli_relaunch.py`, `tests/test_cli_elevation.py` and `tests/test_mode_elevation.py`. Move any
  case that checks something still true (for example "an elevated run refuses a user-writable gupkg tree") into the
  tests of `installation.py` before deleting.

## Change

1. **One trust method.** Add `Installation.require_trusted(path, what)`:
   - on the user side it does nothing;
   - on the system side it raises `UntrustedLocationError` when `system.directory_untrusted_reason(path)` gives a
     reason.

   Replace the `if elevated:` / `is_elevated_run(system)` checks with it in: `engine/context.py`
   (`require_work_folder`, the cache, the tool check), `engine/locks.py`, `services/hooks.py` (`require_trusted_code`),
   `services/gitenv.py`, `services/registrysource.py`. Where a function only has `system`, pass the installation
   instead.
2. **The machine-only environment** for `${NAME}` (D19) is chosen by `installation.side == "system"`.
3. **`is_elevated_run`** is called in exactly one place: `build_installation`. `selfmgmt/elevation.py`'s
   `enforce_bundled_runtime` at start-up keeps its own check (it runs before the installation exists). Write that
   exception in your log.

## New tests (each must fail on `main`, then pass)

- [ ] A non-elevated `install` of a package whose `modes = ["system"]` exits 2 with the T02 message. No process is
      started: `FakeSystem` records no launch.
- [ ] Every result document has `"api": 2` and no `relaunched` key.
- [ ] On the system side, a hook folder that `FakeSystem` marks user-writable is refused through `require_trusted`. On
      the user side the same folder runs.

## Done when

- [ ] `git grep -n -i "relaunch\|runas\|ShellExecute\|batch-confirmed\|stop-file\|mixed-modes\|child-no-result\|child_raw"`
      prints nothing in `src`.
- [ ] `git grep -n "is_elevated_run\|is_elevated()" -- src` matches only `installation.py`, `selfmgmt/elevation.py`, the
      `System` implementations, and `bootstrap.py`. List each match in your log.
- [ ] The full suite passes.
