# T13: Area `services`, part 2: unpack and hooks

Issue 036 F4, F6. Follow the brief §4a.

## Scope

`tests/test_unpack.py`, `test_unpack_bombs.py`, `test_unpack_guards.py`, `test_extractors.py`,
`test_distribution_copy.py`, `test_hooks.py`, `test_hook_trust.py`, and the unpack, prepare and HTTP parts of
`test_service_details.py`.

## Target layout

```text
tests/services/
  test_unpack.py          kinds, options, hostile archives, bombs and limits (services/unpack)
  test_unpack_xz.py
  test_extractors.py
  test_hooks.py
  test_hook_errors_and_trust.py
tests/engine/
  test_distribution_copy.py   (distcopy belongs to the engine area)
```

## Findings to act on (review of 2026-10-30; verify each)

- **`test_unpack_guards`** exists "so removing any single [guard] fails a test":
  - 4 tests call the private `_FolderWatch` (checked);
  - `test_each_nested_archive_receives_the_budget_with_what_earlier_ones_used` spies on `extract_archive` and repeats
    `test_unpack_bombs::test_limits_are_cumulative_across_nested_archives`;
  - its zip per-file and total tests repeat `test_unpack::test_a_zip_bomb_is_rejected_by_its_declared_size` and
    `test_a_zip_with_too_many_entries…`;
  - `test_padding_between_two_xz_streams_must_be_a_multiple_of_four_bytes` repeats the parametrised xz test above it.

  Each user-visible limit gets one test through the public entry point. Retire the rest.
- **`test_unpack_bombs`:**
  - `test_the_default_entry_limit_is_two_hundred_thousand` only pins a constant (as does
    `test_distribution_copy::test_the_default_limits_are_the_unpack_limits`). Keep them only if the limit is documented
    to users;
  - a `Path.unlink` spy.
- **`test_distribution_copy`:** its TOCTOU tests patch `os.*`. That is a real boundary, so keep them.
- **`test_hooks`:**
  - three tests repeat `test_pkgcheck` (T16): `test_a_version_directory_hook_that_uses_the_network_directly…` and
    `test_update_discovery_is_rejected_in_a_version_directory` (2), which T16 also covers;
  - `test_the_context_is_given_to_a_non_python_script_as_json` is covered by
    `test_a_non_python_populate_script_receives_the_context_as_json_on_standard_input`;
  - `test_hooklib_find_digest_reads_a_sums_file` repeats checksum discovery (T12).
- **`test_hook_trust`:**
  - `test_a_hook_that_exits_while_loading…` and `test_any_exception_in_a_hooks_import_time_code…` should be one
    parametrised test;
  - `test_a_hook_folder_that_is_user_writable_is_refused_through_require_trusted…` repeats the first and third tests.
- **`test_service_details`** is a grab bag, and includes a test that searches the source for `subprocess\.run`
  ("issue 021"). Put its parts into their features; retire the source search.

## Done when

Everything in the brief §4a.
