# T08: Area `catalog` (with scope and settings)

Issue 036 F3–F8. Follow the brief §4a.

Area `catalog` = `src/gupkg/catalog/` plus `targets`, `scope`, `settingsload`, `settingsedit` and `viewdata`
(`_docs/tests.md` §7): what is installed where, under which root and settings.

## Scope

All `tests/test_catalog_*.py` (7 files), `tests/test_targets.py`, `tests/test_scope.py`, `tests/test_scope_model.py`,
`tests/test_scope_isolation.py`, `tests/test_install_record.py` and `tests/test_settings_layers.py`.

## Target layout

```text
tests/catalog/
  test_state_table.py      catalog_cases (the table-driven model) + what is left of catalog_state
  test_resolve.py          selectors + targets
  test_registry.py         catalog_registry
  test_links_and_trust.py  catalog_security + catalog_hardening + catalog_one_root
  test_install_record.py   install_record (catalog/record)
  test_scope.py            scope + scope_model
  test_isolation.py        scope_isolation (its main test)
  test_settings.py         settings_layers (loading and layering; the CLI's settings commands stay in T09)
```

## Findings to act on (review of 2026-10-30; verify each)

- **`catalog_state` repeats rows of `catalog_cases`.** `catalog_cases` is the good model: add a row there, not a test.
  The repeats:
  - `test_local_package_follows_the_registry_by_name_unless_it_opted_out` (3 params) repeats rows RVT-linked-installed-registry-newer,
    RVT-opted-out and RVT-explicit-link-to-another-entry;
  - `test_tracker_and_two_versions_without_current_is_local…` repeats T-tracker-and-versions;
  - `test_local_directory_that_differs…diverged` repeats RVT-local-copy-differs;
  - `test_package_with_only_a_tracker_reports_no_version_known…` roughly repeats T-tracker-only.
- **An exact copy:** `catalog_state::test_record_mode_that_disagrees_with_the_scope_breaks_the_package` is a copy of
  `catalog_one_root::test_a_record_of_another_mode_is_broken_with_the_reinstall_hint`. This was checked: the setup is
  identical line for line.
- **`catalog_hardening` repeats `catalog_security`:** for example `test_elevated_run_refuses_a_path_in_a_folder_that_users_can_modify`
  repeats `test_elevated_run_refuses_a_path_in_an_untrusted_folder`.
- **`catalog_one_root`:** its `list --json` test belongs to the CLI area (T09). Leave it in a flat file named in your log.
- **`catalog_security`** uses the internal `read_current` and `scan_package`; drive it through `Catalog`.
- **The mode refusals** are tested in `test_scope::test_a_package_is_checked_against_the_mode_of_the_scope` (8 cases),
  in `test_targets` (3 tests) and in `engine_modes` (T07). One owner; say which.
- **integrate = "none"** is tested in 4 places (scope × 1, scope_model × 2, engine_modes × 1). One owner.
- **`test_scope_model`:** the frozen-dataclass test is about implementation. `test_system_integrate_none_is_honoured_too`
  is about history ("no longer forces full").
- **`test_scope_isolation`:** `test_a_mere_exists_of_the_other_root_is_recorded` and `test_the_program_root_carve_out…`
  test the `Touches` recorder, not gupkg. Keep the main isolation test.
- **`test_targets`:** its refusals repeat `engine_modes`, `test_scope` and `catalog_one_root`.
- **The `.gupkg` junction rule:** coordinate with T07, which may already have chosen the owner.
- **`test_install_record`:** its atomic-write test duplicates the engine's store test (T07).
- **`test_settings_layers`:**
  - its 9 "row" tests should be one table, and it has some duplicate pairs;
  - its docstrings cite "schema 3; issue 035, D48, D50".

## Done when

Everything in the brief §4a.
