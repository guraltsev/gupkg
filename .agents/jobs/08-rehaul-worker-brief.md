# Job 08 worker brief: implement one rehaul issue

You implement exactly one issue of the gupkg 1.0 rehaul, on the branch the
controller names, in your own worktree. The controller reviews and merges
your work. You never merge into `main`, and you never push.

## Read first

- `AGENTS.md`, `_docs/python_rules.md`, `_docs/tests.md` and
  `_docs/docstring_schema.md`; for UI work also `_docs/tui_style_guide.md`.
- **Your issue, completely,** including its Tests and Acceptance criteria.
- Every section of `_docs/rehaul_design.md` that your issue cites. The
  decision table in §19 (D1–D46) is binding.
- Your issue's entries in the epic, `_issues/issue020-rehaul-epic.md`.
- The code you will change, and the tests that cover it today.
- If you are continuing earlier work: `.agents/jobs/08-rehaul-logs/issueNNN.md`
  and the branch's commit log.

## Work

1. **Set up.** Check out the branch the controller named (create it from
   the current `main` if it does not exist). Run `scripts\test.cmd` and
   note the result as your baseline.
2. **Plan.** Before editing, write `.agents/jobs/08-rehaul-logs/issueNNN.md`:
   - the files you expect to change;
   - every acceptance criterion, with the test or manual check that will
     cover it;
   - anything in the issue you find unclear.

   Keep the file up to date as you go.
3. **Implement the issue's scope, and only that.** Build on what earlier
   issues created, for example `System` and `FakeSystem` after 021, or the
   event reporter after 022. Do not add a parallel mechanism.
4. **Test it** the way `_docs/tests.md` requires:
   - test behaviour through public boundaries, and mock only real
     boundaries (after issue 021, that means `FakeSystem`);
   - give every test a docstring that follows `_docs/docstring_schema.md`;
   - build fixtures at test time, or record them;
   - mark tests that need real Windows as `windows_integration`;
   - never touch the real machine: no real `C:\opt`, no `PATH` changes, and
     registry keys only in the throwaway locations issue 021 defines.
5. **Cover every acceptance criterion.** Each one maps to an automated test,
   or to a numbered entry in `tests/manual_smoke.md` when it needs real
   Windows, UAC or a VM. Say why for each manual one.
6. **Update user documentation** only as far as the issue's scope says.
7. **Fix obvious text errors, and stop on real decisions.**
   - If the design or issue text is wrong in a way your work makes obvious
     (a stale reference, a wrong fact, a typo), correct it on your branch
     and list the change.
   - If your work needs a decision that the design and issues don't settle,
     or would contradict D1–D46, stop and report the question. Don't guess.
8. **Finish.**
   - `scripts\test.cmd` passes;
   - `git diff --check` passes;
   - commits are focused, and each message ends with the repository's
     attribution trailer for your session.

## Do not

- merge, push, tag, release, or bump the version (the controller does
  releases);
- run downloaded artifacts, or use the network in tests;
- change another issue's scope, renumber issues, or edit
  `.agents/jobs/08-rehaul-progress.md`.

## Report (your final message)

1. The issue, the branch, and the commits (hash and one line each).
2. The test results before and after (passed, failed, skipped).
3. A table of acceptance criteria. Each row gives the criterion, then the
   test (`file::name`) or manual check id that covers it, then its status.
4. The manual checks the owner must run.
5. The design or issue text you changed, and why.
6. Open questions, each marked blocking or not.
7. Anything you left undone.
