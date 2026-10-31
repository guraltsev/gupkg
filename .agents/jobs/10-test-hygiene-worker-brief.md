# Job 10 worker brief: do one task of issue 036

You do **one** task. The task file tells you what to change. You work on the branch the controller names, in your own
worktree. The controller reviews and merges your work. You never merge into `main`, and you never push.

Most tasks of this job **delete and rewrite tests**. Deleting a test is safe only when you can name the test that
protects its behaviour afterwards. That is what the behaviour map (§3) is for. Without a map, do not delete.

## 1. Read first (in this order)

1. `AGENTS.md`. The "Working rules" are mandatory.
2. `_docs/tests.md` and `_docs/tests_hygiene.md`, completely. They are the standard your work is reviewed against.
3. Your task file, completely.
4. `_issues/issue036-test-suite-quality.md`: the sections your task names.
5. Every test file in your task's scope, completely, before you change it. Skim the product code it tests, so you can
   judge what is behaviour and what is implementation.
6. If you continue earlier work: `.agents/jobs/10-logs/Txx.md` and `git log main..HEAD`.

## 2. Set up

1. Check out your branch. If it does not exist, create it from `main`: `git checkout -b i036/Txx main`.
2. Record your baseline:
   - run the full suite: `$env:GUPKG_NO_PAUSE='1'; .\scripts\test.cmd --all` (before T02: `.\scripts\test.cmd`);
   - after T00, also run `tools\test_report.py` and keep its output;
   - for an area task, also run `pytest --collect-only -q <the area's files>` and keep the count.

   `GUPKG_NO_PAUSE=1` is required: without it the script waits for a key and never returns.
3. Write your plan to `.agents/jobs/10-logs/Txx.md`:
   - the files you will change;
   - each item of the task's "Done when" list, and how you will show it;
   - anything unclear.

   Keep this file updated while you work.

## 3. The behaviour map (every task that removes, merges or moves tests)

Before you delete anything, write a table into your log, with one row per test function in scope:

| Old test (`file::name`) | Behaviour it protects (one sentence, Q1) | Verdict | New owner test (`file::name`) |
| --- | --- | --- | --- |

The verdict is one of:

- **keep**: unchanged apart from moving;
- **rewrite**: same behaviour, better test;
- **merge**: it becomes a case or an assertion of the owner test;
- **covered**: another test already fails for the same breakage. Name it;
- **retire**: it protects no behaviour of today's product. That means a history test, a test of private code, a test of
  unused code, or a tautology. Say which.

Rules for the map:

- **Read the old test, not only its name.** Docstrings in this suite sometimes claim more than the test checks (issue 036
  F11). Map what the test really asserts.
- **A `covered` verdict needs evidence.** Read the covering test, and say in a few words why it fails for the same
  breakage.
- **If you cannot name an owner for a real behaviour,** write a new owner test (and prove it fails, §4). Or stop and ask.
- **Parametrised cases** are mapped as one row per class of input, not per case.

The reviewer checks the map by breaking product code and running your owner tests. Make it accurate.

## 4. Writing tests

- **Follow `_docs/tests.md` completely:**
  - area folders and feature files (§3);
  - names and docstrings (§4.1, §4.2);
  - fixtures (§4.3), parametrisation (§4.4) and mocking (§4.5);
  - tiers (§5).
- **Every new or rewritten test is proven:**
  1. break the behaviour it protects, or run it on `main` if `main` lacks the behaviour;
  2. see it fail;
  3. restore, and see it pass;
  4. paste both outputs into your log, short.

  A test that passes either way is worthless. Fix it or delete it.
- **Put temporary scaffolding in `tests/_temp/job10/`** (`tests_hygiene.md` §3). Retire or promote all of it before you
  finish. This job leaves no temporary tests behind.
- **Hygiene baseline.** You may remove lines from `tests/hygiene_baseline.txt`, never add them. An area task ends with no
  baseline line for its area.

## 4a. Area tasks (T07–T17)

An area task cleans one area of the suite in one piece of work (`tests_hygiene.md` §5). The task file lists the files in
scope, the target layout and the findings of the review of 2026-10-30. The findings are a starting point: verify each one
by reading the code before you act on it, and look for more of the same kind.

The procedure:

1. **Map** every test function in scope (§3).
2. **Retire** the tests the map marks `retire`: history tests, tests of private code, tests of unused code, tautologies.
3. **Merge** duplicates into their owner test. The owner is the layer that decides the rule (`tests.md` Q3). Other layers
   keep at most one case showing the rule is wired in.
4. **Cut** over-parametrisation down to one case per class of input, each with an `id=` (`tests.md` §4.4).
5. **Rewrite** tests of internals through the public entry point, or retire them if the behaviour is not observable.
6. **Fix** docstrings that claim more than the test checks: make the test check it, or make the text true.
7. **Rewrite** names and docstrings to `tests.md` §4.1–§4.2:
   - the module docstring describes the feature as a user sees it, with a link to its section of the official
     documentation;
   - each test docstring explains its behaviour;
   - remove history, issue references and restatements of the code.
8. **Compare with the documentation.** Read the official documentation of the area's features (`README.md`, `_docs/`,
   `--help`) against the new docstrings. For each disagreement, find out which side is outdated from the product's
   actual behaviour (`tests.md` §4.2):
   - if the documentation is outdated, fix it;
   - if the test is outdated, fix the test;
   - if the product matches neither, report the bug and do not fix product code;
   - in every case, list the disagreement and its resolution in your log.
9. **Move** the result into the target files under `tests/<area>/`, each with its tier.

Every area task is done only when, in addition to its own list:

- [ ] the behaviour map covers every test function in scope, with totals;
- [ ] no file in scope is left flat in `tests/`, and the target files exist as the task lists them (or your log says why
      a file differs);
- [ ] `tests/hygiene_baseline.txt` has no line for any file of the area;
- [ ] the list of disagreements between docstrings and documentation (step 8) is in your log, each one resolved or
      reported as a bug;
- [ ] the serial wall time of the area's tests, per tier, before and after, with the 5 slowest tests and why each is
      slow. A time is measured and reported, never a pass/fail limit (`tests.md` §5);
- [ ] the counts are recorded: test functions and collected cases, lines and characters of the area's tests, before and
      after;
- [ ] `scripts\test.cmd --all` passes.

## 5. Product code

Do not change `src/` unless your task says so. If a test can only be made good by changing product code (for example, a
missing seam to inject a clock), stop and ask, with the change you would make.

## 6. Never

- Never touch the real machine:
  - not `C:\opt`, `C:\Users\guraltsev\opt`, `C:\bin` or `%USERPROFILE%\bin`;
  - not the real Start Menu, `PATH` or the Windows registry (outside a `gupkg-test-*` key).
- Never merge, push, tag, release, or bump the version.
- Never edit the progress file, the controller file, this brief or the task files.
- Never use the network in tests.
- Never add a line to the hygiene baseline.

## 7. When to stop

Stop and report, with the question and the options you see, when:

- the task, issue 036 and the policy files do not tell you what to do;
- a behaviour has no owner test, and you cannot write a good one;
- you tried two different fixes for the same failure and both failed (AGENTS rule 10);
- a test you did not touch fails and you cannot tell why.

Do not try a third guess.

## 8. Finish

All of these must be true:

1. Every item of the task's "Done when" list holds. You ran every command it lists, and the output is in your log.
2. `scripts\test.cmd --all` passes (before T02: `scripts\test.cmd`).
3. `git diff --check main...HEAD` prints nothing.
4. Your commits are small and focused. Each message ends with the repository's attribution trailer.

## 9. Report (your final message)

1. Task, branch, commits (short hash and one line each).
2. Test counts before and after, per tier, and the wall time of each tier.
3. `tools\test_report.py` before and after (from T01 on).
4. The "Done when" list. For each item: done or not, and the test or command that shows it.
5. The behaviour map's totals (keep / rewrite / merge / covered / retire), and the path of the map in your log.
6. For each new or rewritten test that is not a pure move: its output with the behaviour broken and restored.
7. Manual checks the owner must run, if any, added to `tests/manual_smoke.md` as `036-Txx-N`.
8. Inconsistencies you found while working (AGENTS rule 13). List them; fix them only if your task covers them.
9. Open questions, each marked blocking or not.
10. What you left undone, and why.
