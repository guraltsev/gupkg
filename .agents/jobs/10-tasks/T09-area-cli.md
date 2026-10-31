# T09: Area `cli`

Issue 036 F3–F8. Follow the brief §4a.

## Scope

All `tests/test_cli_*.py` (10 files), and the CLI tests that T07 and T08 left in flat files (named in their logs).

Area `cli` = `src/gupkg/cli.py`: the commands, their arguments, their output and exit codes. What a command does
underneath belongs to the area of that code, and is tested there.

## Target layout

```text
tests/cli/
  test_grammar.py
  test_machine_interface.py
  test_lifecycle.py         + cli_distribution_install + cli_install_options
  test_scope.py             only --root, the untrusted tree, the self-update root, system-only
  test_reading.py
  test_registry.py          the registry commands of cli_tools
  test_settings.py          the settings commands of cli_tools (loading and layering is tests/catalog/test_settings.py)
  test_pkg.py               cli_adopt + pkg new / check / pin / export / bundle
  test_self.py              the self commands
  test_launcher_encoding.py
```

## Findings to act on (review of 2026-10-30; verify each)

- **Removed spellings are tested many times:**
  - `test_cli_grammar::test_the_old_commands_are_unknown` (5 cases, "0.17 … D33") and `…removed_options…` (10 cases);
  - `--mode` is refused in `test_options_go_after_the_command…`, in `test_cli_scope::test_the_mode_option_and_the_selector_prefixes…`
    (7 cases) and in machine_interface's usage example.

  Keep one test that an unknown command is a usage error, and one that an unknown option is.
- **"The token decides the mode" is tested 3 times:**
  - `test_cli_lifecycle::test_the_mode_is_the_tokens…`;
  - `test_cli_lifecycle::test_an_elevated_gupkg_installs_a_registry_package…`;
  - `test_cli_scope::test_a_normal_process_installs_into_the_user_root…`.

  The resolve line is checked again in `test_cli_scope::test_the_resolve_line…` and in `test_run_logs` (T11).
- **Skipping a checksum** is covered by 3 overlapping tests in grammar, and there are 2 duplicate pass-through refusals.
- **Machine interface:**
  - the "no relaunch key" tests (`test_every_result_document_says_api_2_and_has_no_relaunch_key` ×5 and
    `test_a_plan_document_has_no_relaunch_key_either`) are history. `test_the_result_document_has_the_pinned_keys` already
    pins the key set;
  - `test_exit_codes_2_3_and_5_are_distinguished` repeats codes other tests assert;
  - the malformed-hash setup is copied 3 times;
  - a variable named `check_updates` holds an `install --plan` result;
  - a test patches the private `lifecycle._dispatch`.
- **`test_cli_tools`:**
  - its BOM test is a test of `tomlio`, so it goes to the model area (T16);
  - a variable named `refused` holds a successful run in `test_registry_sync_writes_the_cache_of_the_scopes_root` and in
    `test_settings_set_writes_the_file_of_the_scopes_root`.
- **`test_cli_launcher_encoding`:** the file and pipe bodies are copy-paste.
- **`test_cli_lifecycle`:**
  - 4 prompt tests can be 2;
  - its docstring says the mode prints as `system (auto: elevated)`, while the test asserts
    `(the installed system gupkg)`. Make them agree with the product.
- **The overwrite option tests from `engine_unowned_files`** (from T07) belong in `test_lifecycle` or `test_grammar`.

## Done when

Everything in the brief §4a.
