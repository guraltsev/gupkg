# T14: Areas `system` and `self` (launchers)

Issue 036 F4, F6, F7, F11. Follow the brief §4a. These tests touch real Windows objects: check machine safety (brief §6)
on every rewritten test.

## Scope

- **System:** `tests/test_system_contract.py`, `test_fake_system.py`, `test_windows.py`, `test_shell_link.py`,
  `test_shortcut_admin.py`, `test_shortcut_modes_on_update.py`, `test_junction_replace_while_running.py`.
- **Launch:** `test_launcher.py`, `test_package_launcher.py`, `test_package_root_resolution.py`, `test_gupkg_shims.py`,
  `test_wrapper_scripts.py`, `test_bundled_mode.py`, `test_elevated_runtime.py`.
- **From T10:** the `gupkg.system.port` test.

## Target layout

```text
tests/system/
  test_contract.py          the true contract only: the same checks on FakeSystem and WindowsSystem
  test_windows_process.py   run, watcher, regex worker process, jobs, output lines, test_windows
  test_windows_links.py     shell_link, junctions, replace-while-running
  test_fake.py
tests/self/
  test_gupkg_cmd.py         launcher + wrapper_scripts
  test_shims.py
  test_package_launchers.py
  test_bundled_mode.py
  test_runtime_trust.py     elevated_runtime (selfmgmt/elevation)
tests/engine/test_shortcuts.py   shortcut_admin + shortcut_modes_on_update (shortcuts are engine effects)
```

Area `self` = gupkg itself: `selfmgmt/`, `launchers/`, `bootstrap`, the shims, the bundled runtime and self-update
(`_docs/tests.md` §7). T15 adds the runtime and self-update tests.

`test_package_root_resolution` goes to `tests/model/test_versions.py` (T16). Leave it in a flat file named in your log.

## Findings to act on (review of 2026-10-30; verify each)

- **Argument forwarding is tested about 70 times:**
  - `test_launcher::test_gupkg_cmd_hands_python_exactly_the_arguments_it_was_given` (13 × 2);
  - `…never_runs_what_a_quoted_argument_contains` (10 × 2);
  - manager / tui (2 × 2);
  - `test_gupkg_shims::test_an_installed_shim_hands_the_program_exactly_the_arguments…` (15 cases, nearly the same
    table);
  - the "outer" parameter repeats `test_wrapper_scripts`.

  Keep one table of argument *classes* (quotes, `=`, `%`, `^`, `&`, empty, spaces, non-ASCII), run once per launcher, as
  one `cmd` run per launcher where possible.
- **Stale docstring:** `test_launcher.py:263` calls the outer `gupkg.cmd` "the target of the shims"; the shims target
  `python.exe`.
- **The wrong-interpreter refusal is tested at four levels:**
  - `test_launcher::test_bootstrap_refuses_to_run_on_another_interpreter` (2);
  - `test_python_dash_m_gupkg_refuses_a_foreign_interpreter`;
  - `test_elevated_runtime::test_runtime_refuses_an_interpreter_that_is_not_the_bundled_one` (2) and
    `…without_isolation_or_with_site` (2);
  - `test_bootstrap_refuses_a_run_on_the_wrong_interpreter_without_importing_the_cli`.

  The `elevated_runtime` versions mock `sys.executable` / `sys.flags` and the private `_admin_signal`,
  `_token_elevation_signal` and `_load_elevation`. Keep the real subprocess tests.
- **`test_elevated_runtime::test_ordinary_run_on_the_isolated_bundled_interpreter_needs_no_admin_only_folders`** asserts
  `require_trusted_directory.assert_not_called()`, which is a call graph.
- **`test_system_contract`** has about 15 Windows-only tests that spy on private internals:
  - `test_the_job_handle_is_closed_on_every_path` counts `_ProcessJob.close`;
  - `test_the_regex_worker_runs_in_a_kill_on_close_job_and_an_ordinary_child_does_not` spies on `_ProcessJob.around`;
  - several tests patch `_REGEX_WORKER` and `_WORKER_START_SECONDS`;
  - `test_a_regex_worker_that_never_starts_is_stopped` and
    `test_a_worker_that_overruns_its_start_limit_is_reported_as_not_started_with_that_limit` are the same scenario.

  The behaviour (no orphan child or grandchild) is in `test_windows_aborted_child_is_gone_and_so_is_a_grandchild_it_started`
  and `test_the_regex_worker_is_killed_at_its_deadline_and_is_gone`.
- **`test_system_contract::test_a_helper_started_by_a_normal_run_is_left_running_when_nothing_went_wrong`** takes 9 s.
  Find what it waits for, and make it cheaper or justify it in the log.
- **`test_package_launcher`:**
  - its template-text tests (`test_the_launcher_template_has_no_root_forwarder…`, which is history, and
    `test_all_launchers_come_from_one_template…`);
  - single-assertion exit-code / pause tests;
  - `test_a_gupkg_exe_on_path_is_found_too` asserts only "not 9009" by running `whoami.exe`, so it would pass for any
    program. Make it check that gupkg ran.
- **`test_shell_link`:** the round trip repeats the contract, and there are two threading tests.
- **`test_shortcut_admin`** starts with a UTF-8 byte-order mark. Remove it.
- **Keep `test_junction_replace_while_running`** (`AGENTS.md` rule 6). Its missing failure path is T19's.

## Done when

Everything in the brief §4a, plus:

- [ ] The argument-forwarding tests start at most 2 `cmd` processes per launcher kind. Paste the count of process starts
      before and after (patch `subprocess.Popen` to count, in a scratch run).
