# T16: Areas `model` and `registry`

Issue 036 F4, F5, F7. Follow the brief §4a.

## Scope

- **Manifest:** `tests/test_manifest_format2.py`, `test_pkgcheck.py`, `test_track_validation.py`.
- **Model:** `test_patterns.py`, `test_jsonpath.py`, `test_model_version.py`, `test_version_names.py`,
  `test_toml_writers.py`, `test_tomlio_tables.py`, and `test_package_root_resolution.py` (from T14).
- **Pattern matching:** `test_regex_guard.py`, `test_regex_runner.py` (what is left after T06).
- **Registry:** `test_registry_source.py`, `test_registry_tool.py`, `test_registry_validate.py`, `test_fingerprint.py`.
- **From T09:** the `tomlio` BOM test.

## Target layout

```text
tests/model/
  test_manifest.py           manifest_format2 (model/manifest)
  test_track_validation.py   (model/trackspec)
  test_pkgcheck.py
  test_pinning.py            (from T07)
  test_versions.py           model_version + version_names + package_root_resolution
  test_patterns.py           patterns + regex_guard
  test_jsonpath.py
  test_toml.py               toml_writers + tomlio_tables
tests/services/
  test_regexrun.py           regex_runner (services/regexrun; process tier for the process cases)
tests/registry/
  test_tool.py
  test_source.py
  test_validate.py
  test_fingerprint.py
```

## Findings to act on (review of 2026-10-30; verify each)

- **History tests in `test_manifest_format2`.** They all prove one rule, that an unknown key is reported:
  - `test_every_0_17_spelling_is_an_unknown_key` (11);
  - `test_origin_versions_table_is_refused`;
  - `test_the_removed_keys_of_the_portable_table_are_unknown` ("owner answer Q6");
  - `test_the_per_mode_tables_are_unknown_keys`;
  - `test_the_package_data_variable_no_longer_exists`;
  - `test_ignore_checksum_is_not_part_of_the_format…`, also in `test_checksum_policy`.

  Keep one test of the rule, with a few classes of unknown key.
- **`test_manifest_format2`:** 4 name-validation tables of about 40 cases. `test_files_without_format_2…` repeats
  `test_pkgcheck::test_format_1_manifest_gets_only_the_generic_error`.
- **`test_pkgcheck`:**
  - 29 cases of Python binding forms of `track` (walrus in a decorator, match-mapping-rest, `type track = int`). That
    tests an AST helper; keep 3–4 cases, one per class;
  - "a track table in a version directory names `_check_updates`" is tested in four places (manifest_format2, pkgcheck ×2
    and hooks ×2; see T13). One owner.
- **Hostile version names:**
  - `test_version_names` has about 83, one case each, and `test_model_version::test_unsafe_names_are_not_versions`
    repeats the list. One case per class;
  - `test_model_version::test_underscore_bootstrap_is_reserved…` ("0.17") is history.
- **`test_regex_runner`** (issue 036 F7):
  - the 76 provider cases (`test_a_provider_that_uses_a_pattern_asks_the_worker_and_fails_closed_when_the_worker_breaks`)
    cover every provider × every breakage. Keep one breakage per provider, plus the breakage kinds once, at the runner;
  - the 19 hostile-corpus cases (`test_a_hostile_pattern_on_its_page_is_bounded_by_the_deadline`) all prove "the deadline
    kills". The exponential cases (4) and the large-page cases (8) already do. Keep one per class of hostile pattern;
  - the cases that do not need a fresh process go through one worker.
- **`test_fingerprint`:**
  - `test_the_fingerprint_of_a_known_tree_never_changes` and `…follows_the_documented_algorithm` pin the same thing;
  - verifying a copy against its fingerprint is tested 3 times (two fingerprint tests and
    `test_registry_tool::test_a_written_version_directory_verifies_as_a_distribution_copy…`).
- **`test_registry_tool`:** the copy-verify test repeats fingerprint.
- **`test_tomlio_tables::test_the_writer_restores_tomli_w_even_when_it_fails`** patches the private `tomli_w._writer`.

## Done when

Everything in the brief §4a.
