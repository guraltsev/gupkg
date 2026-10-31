# T20: Acceptance

Issue 036 §5.

## Goal

Show that issue 036's "Done when" holds, compare the indicators with the baseline, and leave the job ready for the owner to
close.

## Do

1. **Run each tier serially, and record the counts and the wall time:**
   - `scripts\test.cmd`;
   - `scripts\test.cmd --process`;
   - `scripts\test.cmd --all`.
2. **Run `tools\test_report.py --timings`** on a full run. Put it beside the baseline of the progress file in one table:
   - the size of the test code and its ratio to the source;
   - test functions and cases;
   - the share of docstrings;
   - the time per tier;
   - the number of baseline lines;
   - the number of temporary folders.
3. **Check each item of issue 036 §5** and show the command or test for each:
   - every module has a tier;
   - `tests/hygiene_baseline.txt` is empty (keep the file with its header, or delete it and the plugin's baseline
     support; ask the owner which);
   - no flat test file is left;
   - no test module imports another;
   - no registry data is in `tests/fixtures/`;
   - F2–F11 are fixed;
   - F12 is covered (T19).
4. **Walk the workflows** of `AGENTS.md` rule 12 against the tests: for each workflow, name the tests that cover it and
   the tier they run in. List any workflow with no test.
5. **Do one more audit pass** (`tests_hygiene.md` §4): read 15 tests chosen at random against `tests.md` §2.1, and list
   any failure.
6. **Write the owner's manual checks**, if any are owed, as `036-T20-N`.
7. **Run the exit review** (`tests_hygiene.md` §3.4) for this job. `tests/_temp/job10/` must not exist.

## Done when

- [ ] All of the above is in your log, and the table of indicators before and after is ready for the issue.
- [ ] `unit` takes under 30 s serially.
- [ ] Nothing is left in `tests/_temp/`.
- [ ] `scripts\test.cmd --all` passes.
