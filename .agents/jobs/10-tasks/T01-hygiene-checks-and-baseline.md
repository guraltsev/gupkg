# T01: Hygiene checks, the baseline, and temporary-test folders

Issue 036 F3, F6, F8, F13; `_docs/tests_hygiene.md` §3, §5, §6, §8.

## Goal

The automatic checks of `tests_hygiene.md` §6 run with every pytest session. Today's violations are recorded in
`tests/hygiene_baseline.txt`, so the suite stays green and every **new** violation fails. Temporary-test folders get
their rules.

This task does not add tiers (T02 does): the tier checks are added in T02.

## Add

1. **`tests/support/hygiene.py`**, loaded as a pytest plugin from `tests/conftest.py`. At collection time it checks:
   - **H1:** no test module imports from another test module (`tests.test_*`, `tests.<area>.test_*`) or from
     `tests._temp`;
   - **H2:** no test module imports a private name (`_x`) from `gupkg`, `gupkg_ui`, `gupkg_tui`, `gupkg_manager` or
     `gupkg_registry`, and no test reads `gupkg.<module>._x`;
   - **H3:** no permanent test's name or docstring contains an issue, decision, task, review or old-version reference
     (pattern: `issue \d+`, `\bD\d{2}\b`, `\bT\d{2}\b`, `round \d`, `reviewer`, `0\.1[0-9]`, `Q\d+`);
   - **H4:** every parametrised case has an explicit `id=` (detect pytest's generated ids, such as `arg0`, `value1`, or a
     bare repr);
   - **H5:** every permanent test module and every test function has a docstring, and its first line is a one-sentence
     summary (`tests.md` §4.2). There is no length limit;
   - **H6:** every folder `tests/_temp/<ref>/` has a `README.md` with `ref:`, `kind:`, `purpose:` and `fate:`; its `ref`
     is open (`tests_hygiene.md` §8); a `kind: report` folder is not collected unless named on the command line.

   A violation is identified by `rule|file|test` (with `test` empty for module-level rules). If it is listed in
   `tests/hygiene_baseline.txt`, it passes; otherwise the session fails with a message naming the rule, the file, the
   test and the section of the policy. A baseline line that no longer matches any violation also fails, so the baseline
   cannot go stale. The message tells the author to delete that line.
2. **`tests/hygiene_baseline.txt`**, generated once by `python -m tests.support.hygiene --write-baseline`, sorted, with a
   header comment saying that it may only shrink. Remove the `--write-baseline` option again after generating the file,
   or make it refuse to run while the file exists.
3. **Temporary tests:**
   - in `pytest.ini`, remove `_devel` from `norecursedirs`;
   - `tests/_temp/` exists with a `README.md` that points to `_docs/tests_hygiene.md` §3;
   - if `tests/_devel/` exists and holds anything, list it in your log and stop: the owner decides its fate.
4. **Tests of the checker** in `tests/support/test_hygiene.py` (for now without a tier). One test per rule: a small fake
   test tree under `tmp_path` that breaks the rule fails with that rule's message, and the same tree with the line in a
   baseline passes. One more test: a stale baseline line fails.

## Do not

- Fix any violation in existing tests: that is the area tasks' work.
- Move existing files.

## Done when

- [ ] `scripts\test.cmd` passes on the branch with the baseline in place. Paste the baseline's line count per rule.
- [ ] Adding a new test that imports a private name, and a new test named `test_issue_99_fix`, each fail the session
      with a message naming the rule. Show both, then remove them.
- [ ] A folder `tests/_temp/issue999/` without a README fails, and with a README whose `ref` is a closed issue fails. Show
      both.
- [ ] `tools\test_report.py` prints the baseline count.
