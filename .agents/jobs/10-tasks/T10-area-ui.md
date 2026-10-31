# T10: Area `ui` (with `gupkg_tui` and `gupkg_manager`)

Issue 036 F3–F8, F10. Follow the brief §4a. UI tests render real screens: `AGENTS.md` rule 8 applies to any test whose
screen output you change.

## Scope

All `tests/test_ui_*.py` (13 files) and `tests/test_manager_*.py` (3 files).

## Target layout

```text
tests/ui/
  test_client.py           ui_client + ui_process_tree
  test_follow.py
  test_render.py
  test_policy.py           ui_plans + ui_steps; the checksum policy as the UI applies it
  test_flows.py
  test_reads.py
  test_startup.py
  test_screens_manager.py  split from ui_screens
  test_screens_tui.py      split from ui_screens
  test_boundaries.py
  test_manager_cli.py      manager_cli
  test_manager_real_cli.py manager_real_cli (process tier)
```

`gupkg_tui` and `gupkg_manager` are thin programs over `gupkg_ui`, so they share its area (`_docs/tests.md` §7).

`test_manager_policy.py` is resolved by T18 (unused code). Until then, keep only its tests of code that the product calls,
and move them to `tests/ui/test_policy.py`.

## Findings to act on (review of 2026-10-30; verify each)

- **The checksum policy table is tested 4 times:**
  - `test_manager_policy::test_the_manager_policy_matrix` (12 cases) and `test_an_invalid_checksum_…` (6);
  - `test_ui_plans::test_a_missing_https_checksum_follows_the_policy_table` (7), `…plain_http…` (6) and `…malformed…` (6);
  - `test_ui_flows::test_the_policy_decides_…` (5) and `…malformed_hash_and_an_http_url…`.

  The owner of the policy is `gupkg.model.manager_policy.decide_target` (the T12 checksum tests own the engine side).
  Test it once, thoroughly, and test the UI's wiring once.
- **Tests of unused code:**
  - `test_manager_policy`: `test_a_batch_asks_once…`, `test_a_yes_applies…`, `…has_no_command_line`,
    `…crosses_a_real_process_boundary…` and `test_a_refused_target_starts_no_process` test `decide_targets` and
    `command_line`, which nothing in `src` calls (checked);
  - the process-boundary test uses its own `argparse` parser, not the CLI's;
  - the refused-target test cannot fail.

  Retire them in the map. T18 removes the code.
- **Clones:** `test_ui_name_conflict` and `test_ui_unowned_file` differ only in a hard-coded message and code. They prove
  only that a CLI error shows on the run screen, which `test_ui_screens::test_cli_error_text_with_markup_is_shown_literally_everywhere`
  and `test_with_colour_off…` already prove.
- **The shell-unsafe argument rule** is tested in 5 places:
  - `test_ui_client` (12 cases), `test_a_refused_argument_starts_nothing`, and `test_plain_values_pass…` (which tests
    `gupkg.system.port`, so it goes to T14);
  - `test_ui_flows::test_a_hostile_package_name_fails_alone…`;
  - `test_manager_real_cli::test_a_package_folder_with_shell_characters…`.
- **`test_ui_screens`:**
  - 4 status-line scope tests and 2 registry-hint tests overlap;
  - one renderer unit test belongs in `test_render`;
  - it reads Textual's private `app.screen._compositor`.
- **`test_ui_flows`:** a duplicate of the policy matrix, and the cleanup test exists twice.
- **`test_ui_boundaries`:**
  - keep the import ban: it is a named security guard (`tests.md` §4.5, R5 exception);
  - retire the function-local-import assertion (style) and `test_the_programs_exist_and_are_scanned`;
  - its AST scan of markup duplicates 3 behaviour tests, so keep it only as a guard.
- **`test_ui_startup`:** 3 "elevated untrusted" refusals that are one rule.
- **`test_ui_process_tree`:**
  - `test_a_success_without_run_end_is_not_believed…` uses no process tree and duplicates
    `test_ui_client::test_an_events_file_without_run_end_is_a_failure`;
  - its last test belongs in the client tests.
- **`test_manager_real_cli`:** its JSON test duplicates `manager_cli`.
- **Docstrings that are wrong:**
  - `test_ui_flows` and `test_ui_client` call `test_manager_cli.py` "the real CLI";
  - `test_ui_client::test_a_good_call_is_ok…` says "api 1";
  - `test_ui_screens` says "Owner answer Q14";
  - `test_manager_policy` says "the manager program itself does not exist yet (issue 030)".

## Done when

Everything in the brief §4a, plus:

- [ ] For every screen test you rewrote, the rendered screen (pilot / `export_text`) is in your log, including the hint
      line and the status line (`AGENTS.md` rule 8).
