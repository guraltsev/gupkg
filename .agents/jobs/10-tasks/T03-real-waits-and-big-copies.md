# T03: Remove real waits and whole-registry copies

Issue 036 F2, §4 decision 2.

## Goal

The two fixes the owner agreed to: no test waits for a real retry delay, and `test_registry_validate` checks each rule on a
small registry instead of a copy of all 79 packages.

## Change

1. **Real retry delays.** Pass a no-op `sleep` (the HTTP client and the engine context accept one) in:
   - `tests/test_registry_tool.py`:
     - `test_the_exit_status_is_one_when_a_package_fails_and_two_without_a_registry_file`;
     - `test_a_staging_folder_that_cannot_be_removed_is_a_warning_not_a_failure`;
     - `test_a_run_removes_the_staging_folders_an_interrupted_run_left_but_nothing_else`;
   - `tests/test_record_http_fixtures.py`:
     - `test_a_failing_run_leaves_the_case_file_untouched`.

   If the registry tool's entry point offers no way to inject `sleep`, stop and ask (product change). Then search the
   whole suite for other tests where a real `time.sleep` of the HTTP client or the engine runs (profile with
   `python -m cProfile`, or patch `time.sleep` to record callers in a scratch run). List them, and fix those that are the
   same pattern.
2. **`tests/test_registry_validate.py`:**
   - one test validates the clean `registry_100` snapshot once (session-scoped). It is replaced in T06;
   - every "this rule finds this fault" case runs on a small registry of 2–3 packages, built under `tmp_path` from a
     helper. The helper writes packages written for the test; it does not copy the snapshot;
   - the behaviour map (brief §3) covers every test of the file.

## Done when

- [ ] The four tests take under 0.5 s each. Paste `--durations` for them.
- [ ] `test_registry_validate.py` takes under 5 s in total. Paste the time before and after.
- [ ] Every rule case still fails when its rule is broken. For at least three rules: comment out the rule in
      `src/gupkg_registry/validate.py` in a scratch change, show the failure, and restore.
- [ ] `scripts\test.cmd --all` passes.
