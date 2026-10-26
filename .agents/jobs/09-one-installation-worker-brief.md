# Job 09 worker brief: do one task of issue 035

You do **one** task. The task file tells you exactly what to change. You work on the branch the controller names, in your
own worktree. The controller reviews and merges your work. You never merge into `main`, and you never push.

Follow the task file literally. When it says "remove", remove. When it says "keep", keep. When something is not
covered, do not guess: stop and ask (see "When to stop").

## 1. Read first (in this order)

1. `AGENTS.md`. The "Working rules" are mandatory.
2. Your task file, completely.
3. `_issues/issue035-one-installation-per-run.md`: the sections your task names.
4. `_docs/python_rules.md`, `_docs/tests.md`, `_docs/docstring_schema.md`. For UI work also `_docs/tui_style_guide.md`.
5. Every source file your task names, completely, before you edit it.
6. If you continue earlier work: `.agents/jobs/09-logs/Txx.md` and `git log main..HEAD`.

## 2. Set up

1. Check out your branch. If it does not exist, create it from `main`: `git checkout -b i035/Txx main`.
2. Run the full suite and write the result down as your baseline:
   ```
   $env:GUPKG_NO_PAUSE='1'; .\scripts\test.cmd -q
   ```
   `GUPKG_NO_PAUSE=1` is required. Without it, the script waits for a key and never returns.
   To run one file: `.\scripts\test.cmd tests\test_x.py -q`.
3. Write your plan to `.agents/jobs/09-logs/Txx.md`:
   - the files you will change;
   - each item of the task's "Done when" list, and how you will show it;
   - anything unclear.

   Keep this file updated while you work.

## 3. Do the work

- Change only what the task lists. No drive-by refactors, no renaming outside the task, no reformatting of lines you do
  not otherwise change.
- Match the surrounding code: its names, its docstring style (`_docs/docstring_schema.md`) and its comment density.
- When you delete a feature, also delete:
  - its tests;
  - its help text;
  - its error messages;
  - its mentions in docstrings of the files you touch.

  Use `git grep -n <word>` to find them.
- When the task says a test must fail first:
  1. write the test;
  2. run it on the code **before** your source change and see it fail: `git stash push -- src`, run it, then
     `git stash pop`;
  3. run it after your source change and see it pass;
  4. paste both outputs into your log.

  A test that passes either way is worthless. Fix it or delete it.
- After every source change, run the full suite before you continue.

## 4. Never

- Never touch the real machine:
  - not `C:\opt`, `C:\Users\guraltsev\opt`, `C:\bin` or `%USERPROFILE%\bin`;
  - not the real Start Menu, `PATH` or environment variables in the Windows registry.

  Tests use `FakeSystem`, temporary folders (`tmp_path`) or `--sandbox`. Never run the installed `gupkg` to install,
  update or uninstall anything.
- Never merge, push, tag, release, or bump the version.
- Never edit `.agents/jobs/09-one-installation-progress.md`, the controller file, this brief or the task files.
- Never use the network in tests.

## 5. When to stop

Stop and report, with the question and the options you see, when:

- the task and issue 035 do not tell you what to do;
- doing the task would break something the task says to keep;
- you tried two different fixes for the same failure and both failed (AGENTS rule 10);
- a test you did not touch fails and you cannot tell why.

Do not try a third guess.

## 6. Finish

All of these must be true:

1. Every item of the task's "Done when" list holds. You ran every command it lists, and the output is in your log.
2. The full suite passes: `$env:GUPKG_NO_PAUSE='1'; .\scripts\test.cmd -q`.
3. `git diff --check main...HEAD` prints nothing.
4. Your commits are small and focused. Each message ends with the repository's attribution trailer.
5. For a UI change, you pasted the rendered screen (Textual pilot / `export_text`) into your log, including the top hint
   line and the status line (AGENTS rule 8).

## 7. Report (your final message)

1. Task, branch, commits (short hash and one line each).
2. Test counts before and after (passed, failed, skipped).
3. The "Done when" list. For each item: done or not, and the test name (`file::test`) or command that shows it.
4. For each new regression test: its output without the fix and with the fix.
5. Manual checks the owner must run, if any. Add each to `tests/manual_smoke.md` as a numbered entry `035-Txx-N`.
6. Inconsistencies you found while working: docs against code, help against behaviour, and so on (AGENTS rule 13).
   List them. Fix them only if your task covers them.
7. Open questions, each marked blocking or not.
8. What you left undone, and why.
