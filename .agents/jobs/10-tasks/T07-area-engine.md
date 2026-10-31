# T07: Area `engine`

Issue 036 F3–F8, F11. Follow the brief §4a.

## Scope

All `tests/test_engine_*.py` (19 files), plus `tests/test_prepare.py` and `tests/test_pinning.py`. The engine helpers in
`tests/support/` may change for this area.

Area `engine` = `src/gupkg/engine/` plus `distcopy` (`_docs/tests.md` §7). Tests in scope whose code lives in another
area move to that area's folder.

## Target layout

```text
tests/engine/
  test_lifecycle.py        install, update, uninstall, switch (lifecycle + what is left of cases + version_questions)
  test_check.py            check (engine/health), including the App fingerprint checks from sources
  test_check_updates.py
  test_populate.py         sources + populate (engine/appbuild)
  test_installer.py
  test_origin.py           distribution folders, fingerprints, mark of the web (from engine_distribution)
  test_effects.py          effects + pathentries
  test_ownership.py        name conflicts + the engine part of unowned files
  test_locks_and_batches.py
  test_modes.py
  test_elevated_trust.py   the elevated-run refusals (from engine_security)
  test_plan.py
  test_receipts.py         store receipts and atomic writes (engine/receipts, engine/stateio)
  test_portable.py         the portable hand-over (from engine_store)
tests/services/test_gitenv.py    the 8 git/environment tests of engine_security (services/gitenv)
tests/services/test_prepare.py   test_prepare (services/prepare)
tests/model/test_pinning.py      test_pinning (pinning)
```

## Findings to act on (review of 2026-10-30; verify each)

- **The state table is tested end to end 2–3 times.** `engine_cases` repeats `engine_lifecycle`,
  `engine_distribution` and `engine_check_updates`:
  - `test_case_r_registry_only`;
  - `test_case_r_registry_with_only_a_tracker`;
  - `test_case_linked_registry_newer_is_copied_in`;
  - `test_case_v_local_versions_not_installed`;
  - `test_case_t_installed_after_a_check`.
- **Tests of private code or call counts:**
  - `engine_name_conflicts::test_a_package_installed_earlier_in_the_session_owns_its_names…` (private `_RootNames`);
  - `test_check_of_many_packages_reads_the_records_once` and `test_a_batch_install_of_many_packages_reads_the_records_once`
    (they count `Catalog.installed_effects` calls);
  - `test_the_name_helpers_compare_letter_case…`;
  - `engine_planner::test_diff_effects_*`;
  - `test_the_cleanup_plan_is_made_from_the_facts_it_is_given_and_never_looks_at_the_disk`;
  - `engine_store::test_the_aggregate_digest…`, `test_diff_app_*` and `test_the_track_state_round_trips`;
  - `engine_locks`: the direct tests of `package_lock` and `lock_holder`;
  - `engine_unowned_files`: the three tests that monkeypatch `lifecycle.find_unowned` and `executor.find_unowned`.
- **Over-parametrisation:**
  - `engine_installer::test_an_argument_that_names_a_place_outside_the_stage…` has 34 cases, each a full install,
    without ids. The rule is already tested directly on `expand_arguments`;
  - `test_an_installer_never_runs_from_a_download_that_was_not_verified` has 4 decisions × 2 modes;
  - `engine_pathentries`: the bad-shape list is checked at 4 layers (the validator with 16 cases,
    `test_desired_effects_refuses_the_same_shapes_after_expansion` with 9 × 2 and no ids, the hostile install record
    with 6, and the apply-effect and control-character tests with 10);
  - `engine_effects`: the same refusal at desired / apply / remove.
- **Asserting a constant against a literal:** in `engine_modes`, three refusal tests assert `PORTABLE_REFUSAL == "…"`. The
  same refusals are also in `test_scope` and `test_targets` (T08). Keep one parametrised end-to-end test, in this area or
  in T08; say which.
- **Docstrings that claim more than the test checks:**
  - `engine_check::test_a_dangling_current_and_a_missing_app_are_reported`: no dangling `current` is set up;
  - `engine_check_updates::test_the_registrys_tracker_is_copied_in_and_run`: the recorded hash is not asserted;
  - `engine_version_questions::test_yes_and_the_option_answer_the_broken_versions_question`: the `--no-input` refusal is
    not tested.

  For each, make the test check what it claims. Those are real gaps (issue 036 F12).
- **History-shaped tests:**
  - `engine_locks::test_racing_runs_never_hold_the_lock_together` runs 8 times, and its docstring is about "the first
    version of the release";
  - `engine_unowned_files::test_the_kind_names_after_a_bare_overwrite…` says "gupkg.cmd no longer splits =".
- **Tautology:** `test_pinning::test_a_skipped_download_writes_nothing_into_the_version_directory` (`fetch_artifact` never
  receives that directory).
- **Misplaced tests:**
  - the 5 CLI-parsing tests of `engine_unowned_files` go to the CLI area (T09). Leave them in a flat file named in your
    log for T09;
  - `engine_distribution::test_a_vendor_installer_step_on_a_zip_artifact…` goes to `test_installer`;
  - the "needs a real run" sandbox test exists in both `engine_security` and `engine_installer`; keep one.
- **`engine_populate`** calls the internal `populate_version` with 9 arguments. Drive it through the lifecycle or the
  smallest public entry point.
- **The `.gupkg` junction rule** is tested 3 times (`install_record`, `catalog_security`, `engine_security`). Keep the
  engine one or the catalog one, and coordinate with T08 in your log.

## Done when

Everything in the brief §4a, plus:

- [ ] The three tests whose docstrings over-claimed now check what they claim, each proven (brief §4).
- [ ] The 34-case installer argument test is at most 6 cases, each with an `id=` naming its class of input.
