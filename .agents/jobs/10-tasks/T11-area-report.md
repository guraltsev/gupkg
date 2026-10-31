# T11: Area `report` (events, logs, console output)

Issue 036 F4, F5, F7, F11. Follow the brief §4a.

## Scope

`tests/test_run_logs.py`, `tests/test_console_renderer.py`, `tests/test_event_contract.py`,
`tests/test_failure_reports.py`, `tests/test_download_progress.py`, `tests/test_process_output_lines.py`.

## Target layout

```text
tests/report/
  test_events.py      event_contract + the events-file tests from run_logs and cli_machine_interface
  test_console.py     console_renderer + download_progress
  test_run_logs.py
  test_failures.py    failure_reports
  test_process_output.py
```

## Findings to act on (review of 2026-10-30; verify each)

- **The events file is tested 5 times:**
  - name validation in `test_run_logs::test_events_file_names_that_are_not_plain_file_names_are_refused` (25 cases) and
    in `…collide_with_the_logs_own_files…` (7), which test a validator directly;
  - the same through the CLI in `test_cli_machine_interface::test_an_events_file_that_is_not_a_plain_name…` (8) and in
    `test_run_logs::test_the_cli_refuses_an_events_file_with_a_path…`;
  - the content of `run_start` / `run_end` in machine_interface, `test_cli_scope`, `test_run_logs`, `test_event_contract`
    and `test_download_progress`.

  One owner for each; the names are cut to one case per class.
- **Redirected and `--json` output is tested 5 times:**
  - "one 100% line, no `\r`" in `test_run_logs`, `test_download_progress` and `test_console_renderer`;
  - "human text on stderr under `--json`" in `test_cli_machine_interface`, `test_run_logs` and `test_download_progress`.
- **`test_run_logs::test_a_stale_events_file…`** asserts that `"OLD-RUN-MARKER"` is absent but writes `"stale"`, so that
  assertion cannot fail. Make the test check what it says.
- **History tests** in `test_run_logs`.
- **Private patching:** `test_run_logs` patches `cli.running_gupkg_logs_base`. Find a public seam, or stop and ask.
- **`test_event_contract`:** its real-CLI test duplicates others.
- **`test_failure_reports`:** its docstring says "issue 035, item 9".
- **`test_download_progress`:** the redirect test is a duplicate.
- **The resolve line** (T09) is checked again in `test_run_logs::test_an_install_leaves_package_logs…`.

## Done when

Everything in the brief §4a.
