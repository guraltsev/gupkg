# Test hygiene

How a test suite stays good over time: the life of a test, temporary tests during long work, retiring tests, paying off
old debt, and the automatic checks that hold the line. What a good test is, and where it goes, is in
[tests.md](tests.md).

Sections 1–7 are general and meant to work for any project. Section 8 binds them to this project.

## 1. Why hygiene

A suite decays one reasonable-looking addition at a time. Each change adds a test and none removes one. A rule is tested
again wherever it was touched, and fixes from review rounds become tests of their own. Removed features leave their tests
behind. Nobody is to blame for any single step, so the decay must be stopped by **rules that a machine checks** and by
**an exit step that every piece of work goes through**, not by good intentions.

## 2. The life of a test

A test is in exactly one of four states:

| State | Lifetime | Where | Held to |
| --- | --- | --- | --- |
| **Permanent** | Until the behaviour it protects is removed | `tests/<area>/test_<feature>.py` | Every rule of [tests.md](tests.md) |
| **Conditional** | Until a stated condition ends (§2.1) | `tests/_conditional/<name>/` | Every rule of [tests.md](tests.md), plus the folder's README |
| **Temporary** | Until the exit review of one open issue or job (§3) | `tests/_temp/<ref>/` | Determinism (Q6 of [tests.md](tests.md) §2.1) and the folder's README; the other quality rules are relaxed |
| **Retired** | — | Deleted. Git history keeps it | — |

A test changes state only through these steps:

- **Writing a permanent test:** it passes the review against [tests.md](tests.md) §2.1, including proof that it failed
  first.
- **Changing behaviour:** update the owner test of that behaviour. Do not add a second test beside it.
- **Removing code:** remove its tests, fixtures and helpers in the same change. A test of code that no longer exists is
  retired, never "kept just in case".
- **Closing temporary tests:** the exit review of §3.4.
- **Ending conditional tests:** the check of §2.1 fails the suite after the folder's date; the tests are then retired, or the
  condition is confirmed and the date moved.

### 2.1 Conditional tests

A conditional test protects real behaviour that exists only while a stated condition holds: a compatibility path for an old
layout or format, a workaround for an outside tool, support for a version that is still in use. It is not scaffolding for a
piece of work, so it does not belong in `tests/_temp/`, and it does not last for ever, so it does not belong among the
permanent tests.

- **Held to every rule of [tests.md](tests.md):** behaviour, proof that it failed first, a docstring in the product's terms, no
  issue or version number. Only its lifetime differs from a permanent test.
- **One folder per condition,** `tests/_conditional/<name>/`, named after the behaviour (`old_registry_layout`), never after
  an issue.
- **Every folder has a `README.md`** with:
  - `until:` the condition that ends the tests, written so a person can check it (for example, a constant that is removed,
    a version that is no longer supported);
  - `expires:` a date, `YYYY-MM-DD`, by which the condition is checked again;
  - `purpose:` one paragraph: the behaviour the tests protect and why it is conditional.
- **The date makes the condition checked.** After `expires`, the suite fails (rule H7) until someone confirms the condition
  still holds and moves the date, or the condition has ended and the tests are retired together with the code they protect.
  A date is a reminder to decide, not a reason to delete a test that still protects something.
- **Promote when the condition goes away for good:** a conditional test whose condition turns out to be permanent moves to its
  feature file and the folder is removed.

## 3. Temporary tests

### 3.1 When they are allowed

Long work sometimes needs tests that are not yet good permanent tests:

- **Characterisation tests** that pin today's behaviour before a rewrite, so the rewrite can be checked against them;
- **Scaffolding** for a refactor that spans many tasks: tests of an intermediate seam, or of both old and new paths;
- **Reports**: procedural deep-analysis runs that print facts (counts, timings, a comparison of two implementations).
  Their assertions are light sanity checks.

They are not allowed as a way around the quality rules for ordinary work. If the work fits in one change, its tests are
permanent from the start. A test that protects a behaviour which lasts beyond the work, but not for ever, is a conditional
test (§2.1), not a temporary one.

### 3.2 The rules

- **Every temporary test is linked.** Temporary tests live in `tests/_temp/<ref>/`, where `<ref>` names exactly one open
  issue or job. One folder per reference; nothing temporary lives anywhere else.
- **Every folder has a `README.md`** with:
  - `ref:` the issue or job, as a path to its file;
  - `kind:` `guard` (runs with the suite) or `report` (runs only when called explicitly);
  - `purpose:` one paragraph: what the tests protect or measure during the work;
  - `fate:` one line per test module, saying what it becomes at the exit review: `promote to <area>/<file>`, `covered by
    <test>`, or `retire`. The line may say `undecided` while the work runs, but not at the exit review.
- **Guards run in the tier their contents need,** like any other test, so the work is protected while it lasts.
  **Reports** are excluded from normal runs. Run them by their path, with output shown.
- **Temporary tests may break the "should" rules** (Q7–Q10: names and docstrings may mention the issue, and cases may be
  many). They may not break determinism or machine safety.
- **Nothing outside `_temp/<ref>/` imports from it.** A permanent test never depends on a temporary one.

### 3.3 Age

A temporary folder whose reference is closed is an error: the suite fails until the exit review is done. A folder older
than its work's expected length is a warning to the owner. That expected length is written in the README, or defaults to
30 days.

### 3.4 The exit review: closing the work closes its temporary tests

Closing the issue or job includes these steps, in this order:

1. **List** every test in `_temp/<ref>/` with the behaviour it protects (Q1).
2. **Decide** for each, and record the decision in the work's log:
   - **promote**: the behaviour matters permanently and no permanent test covers it. Rewrite it as a permanent test in its
     feature file, to the full policy, and prove it fails without the code it protects;
   - **covered**: a permanent test already protects it. Name that test;
   - **retire**: it only mattered during the work (a seam that is gone, a comparison with deleted code, a report).
3. **Expect most tests to be retired or covered.** Usually only a small part of a temporary suite should be promoted.
   Promoting a test means rewriting it, not moving the file.
4. **Delete** `tests/_temp/<ref>/` entirely.
5. **Close** the issue or job. The automatic check of §3.3 makes the order hard to skip.

## 4. Reviews and audits

- **Every change that touches tests** is reviewed against [tests.md](tests.md) §2.1 by someone other than its author. For
  an agent, that means a different agent with a fresh context. The review names, for each removed test, the test that
  now protects its behaviour, or says the behaviour was retired on purpose.
- **Every release, or every few closed issues,** someone audits the suite:
  - measure the indicators of [tests.md](tests.md) §2.3 and compare them with the last audit;
  - read a sample of recently added tests against §2.1;
  - list the behaviours tested more than once and the dangerous workflows with no test;
  - compare the docstrings of a few feature files with the official documentation of those features, and resolve every
    disagreement ([tests.md](tests.md) §4.2);
  - record the result in the issue tracker. Open an issue when an indicator moved the wrong way.
- **A reviewer's finding becomes a case or an assertion in the owner test,** not a new test named after the review.

## 5. Debt from before the policy, and the ratchet

A suite that existed before this policy will break many rules at once. It cannot be fixed in one change, and new work
should not wait for it. So:

- **A baseline file lists the known violations**, one line each: rule, file, test. The automatic checks (§6) fail on
  any violation that is *not* in the baseline, so new code meets the policy from the first day.
- **The baseline only shrinks.** A change may remove lines from it, never add them. If a change cannot avoid a new
  violation, that is a question for the owner, not an edit to the baseline.
- **Clean up area by area.** Each area is cleaned in one piece of work:
  - map every behaviour its tests protected before to the test that protects it after;
  - remove duplicates, history tests and tests of internals;
  - move the files into the area folder;
  - delete the area's baseline lines.

  The map of behaviours before and after is what makes deleting safe. Without it, do not delete.

## 6. Automatic checks

These run at collection time with the suite itself, so they cannot be skipped. Each failure names the rule and the fix.

| Check | Rule |
| --- | --- |
| Every test module declares a tier | [tests.md](tests.md) §5 |
| A `unit` test does not start a process, sleep or open a socket | §5 |
| No test module imports from another test module or from `_temp/` | §3, §3.2 here |
| No import of a private name (`_name`) from the product's own code | §4.5 |
| No issue, decision, task, review or old-version number in a permanent test's name or docstring | §4.1, §4.2 |
| Every parametrised case has an `id=` | §4.4 |
| Every permanent test module and test has a docstring that starts with a one-sentence summary | §4.2 |
| Every `_temp/<ref>/` has a README with `ref`, `kind`, `purpose` and `fate`, and its `ref` is open | §3.2, §3.3 here |
| Every `_conditional/<name>/` has a README with `until`, `expires` (a valid date) and `purpose`, and its `expires` has not passed (H7); its tests follow every other rule | §2.1 here |
| A violation that is in the baseline passes; a new one fails | §5 here |

A tool also prints the indicators of [tests.md](tests.md) §2.3, so every job can paste a before-and-after.

## 7. Rules for agents running long work

- At the start, record the indicators (§6). At the end, report them again, with the tests added, promoted, covered and
  retired.
- Put scaffolding in `tests/_temp/<ref>/` from the start, never among the permanent tests "for now".
- Fill in each temporary module's `fate:` line as soon as you know it. Do not leave all of them for the end.
- The exit review (§3.4) is a task of the job, with its own review, not a footnote.

## 8. This project (gupkg)

- **References:** `<ref>` is `issueNNN` (file `_issues/issueNNN-*.md`; closed when it moves to `_issues/_closed/`) or
  `jobNN` (file `.agents/jobs/NN-*-controller.md`; closed when its progress file says `Status: Closed`).
- **Report runs:** `scripts\test.cmd tests\_temp\<ref>\<file>.py -s`.
- **Baseline:** `tests/hygiene_baseline.txt`, written once by job 10 task T01 and then only shortened.
- **Indicators:** `tools\test_report.py` (job 10 task T00).
- **Status (2026-10-30):**
  - The checks, the baseline and the tool do not exist yet; job 10 builds them.
  - `tests/_devel/` (the former place for temporary tests, excluded from discovery) is replaced by `tests/_temp/<ref>/`.
  - The suite's current failures are described in
    [issue 036](../_issues/issue036-test-suite-quality.md).
