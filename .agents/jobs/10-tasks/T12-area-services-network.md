# T12: Area `services`, part 1: network, checksums, git

Issue 036 F4, F6, F7. Follow the brief §4a.

## Scope

`tests/test_http_client.py`, `test_http_policy.py`, `test_http_transport.py`, `test_http_hostile_server.py`, `test_tls.py`,
`test_redact.py`, `test_checksum_fetch.py`, `test_checksum_policy.py`, `test_checksum_discovery.py`,
`test_chain_and_checksum_limits.py`, `test_git_services.py`, and `tests/services/test_gitenv.py` (from T07).

## Target layout

Area `services` = `src/gupkg/services/` plus `tls`, `redact`, `hooklib`, `integrity` and `extractors`
(`_docs/tests.md` §7). One file per service.

```text
tests/services/
  test_http_client.py      http_client + http_policy (headers, redirects, tokens, retries)
  test_http_transport.py   http_transport + http_hostile_server + tls; one shared loopback server fixture
  test_redact.py
  test_checksum_policy.py  the gate, the records, the plan
  test_checksum_fetch.py   fetch_artifact, fetch_git, reverify, the real loopback (services/artifact)
  test_checksum_discovery.py  discovery + the optional-source parts of chain_and_checksum_limits
  test_git.py              git_services + gitenv
```

The chain budget part of `test_chain_and_checksum_limits` goes to `tests/services/test_track.py` (from T06).

## Findings to act on (review of 2026-10-30; verify each)

- **Source-text tests in `test_checksum_fetch`:**
  - `test_the_download_rule_and_everything_it_imports_read_neither_settings_nor_the_environment`;
  - `test_the_forbidden_name_check_is_not_vacuous`;
  - `test_the_closure_includes_the_parent_packages_of_every_module`.

  They parse `src/` with `ast` and pin `ENVIRONMENT_READERS`. The behaviour is covered by
  `test_a_checksum_missing_skip_setting_has_no_effect_on_the_download_rule`. Retire them, unless the owner names the
  first one a security guard (stop and ask).
- **"Near-miss digit" families:** 29 cases that prove string `==`. They are:
  - `test_verify_observed_compares_every_digit`;
  - `…differs…in_one_digit…`;
  - `test_a_cached_file_is_reused_only_for_the_exact_digest`, which also rebuilds the private cache key;
  - `test_reverify_compares_every_digit`;
  - `test_a_commit_that_differs_in_one_digit…`.

  Keep one wrong-digest test per entry point.
- **The policy table is tested twice:** `test_checksum_policy::test_the_gate_applies_the_rows_of_the_policy_table`
  (11 rows) and `test_checksum_fetch::test_the_policy_matrix_over_fetch_artifact` (14 rows). Keep the matrix at the fetch
  level.
- **A malformed digest is refused, in 5 places:** policy (11), fetch (6), discovery (4), chain (3) and manifest (T16).
  One owner.
- **The "odd https spelling" rule:** policy `test_is_https_url_judges_the_parsed_address` (24 cases) and fetch
  `test_an_address_that_is_not_plainly_https_cannot_be_skipped` (9). About 8 spellings, one per class.
- **`test_checksum_policy::test_the_decision_values_are_exactly_the_documented_ones`** pins a tuple. Keep it only if the
  values are a documented contract (the README or the JSON document); otherwise retire it.
- **`test_checksum_fetch`:** a frozen-dataclass test.
- **The HTTP files** split header rules, token rules and loopback servers across files, with four handler classes
  (Drip/Hostile/Loopback/Handler). Use one loopback fixture.
- **`test_tls`** only inspects SSL context flags. It is a weak test, but keep it and note the gap (issue 036 F12).

## Done when

Everything in the brief §4a.
