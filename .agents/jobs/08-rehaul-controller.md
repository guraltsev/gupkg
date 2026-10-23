# Job 08: Rehaul controller (implement gupkg 1.0)

Status: Open

Depends on: job 07 (closed, `_complete/07-rehaul-pre-implementation-confirmation.md`).

Design authority:
- `_docs/rehaul_design.md`; its decision table (§19, D1–D46) is binding;
- the epic `_issues/issue020-rehaul-epic.md`;
- issues `_issues/issue016-…` to `_issues/issue033-…`.

Where an issue and the design disagree, the newer owner decision (D21–D46)
wins. If neither settles it, stop and ask (see "Stops").

## Objective

Take the repository from 0.17 to a completed 1.0 rehaul:

- every issue 016–019 and 021–033 is implemented, verified against its
  acceptance criteria, and merged into `main`;
- the epic (issue 020) and issue 032 are closed.

The controller does not implement issues itself. It dispatches one worker
agent per issue, has every result reviewed by a separate agent, merges it,
and keeps a progress file, so that a run can stop and resume at any point.

## Files

- **This file:** the controller's instructions.
- **`08-rehaul-worker-brief.md`:** the instructions every worker receives,
  verbatim.
- **`08-rehaul-progress.md`:** the controller's state: per-issue status,
  branch, merge commit, test counts, pending manual checks, owner questions
  and a log. Only the controller edits it.
- **`08-rehaul-logs/issueNNN.md`:** each worker's own log for its issue:
  plan, acceptance mapping, deviations, questions. Workers create it.

## Required preparation

1. Read `AGENTS.md`, `_docs/python_rules.md`, `_docs/tests.md`,
   `_docs/docstring_schema.md` and `_docs/tui_style_guide.md`.
2. Read `_docs/rehaul_design.md` and the epic completely, and the progress
   file.
3. If the progress file shows an issue `in progress` or `in review`, resume
   it first (see "Resume").
4. On `main`, run `scripts\test.cmd`. Record the result (passed, failed,
   skipped) as the baseline in the progress file. If the baseline fails,
   stop and report.

## Order and parallelism

Issues are taken in the epic's dependency order:

| Issue | Depends on |
| --- | --- |
| 016 | — |
| 017 | — |
| 018 | 016 |
| 019 | 016 |
| 021 | 016, 017, 018, 019 (all of stage 0; D35) |
| 022 | 021 |
| 023 | 019 |
| 024 | 023 |
| 025 | 021, 023 |
| 026 | 021, 022, 023 |
| 027 | 023, 026 |
| 028 | 021, 022, 024, 025, 026, 027 |
| 029 | 028 |
| 030 | 018, 022, 027, 029 |
| 031 | 023, 026, 027 |
| 033 | 016, 023, 026, 028, 029, 030 |
| 032 | all of the above (last; closes the epic) |

An issue is **ready** when every issue it depends on is `merged`. Note that
023 depends only on 019, but D35 still holds: no stage 1 issue (021 onwards)
starts before all of stage 0 is merged.

- **Concurrency.** Run at most two workers at a time, and only on ready
  issues.
- **Priority.** Prefer the critical path: 016 → 018/019 → 021 → 022 → 026
  → 027 → 028 → 029 → 030 → 033 → 032. Use the second slot for the
  issues beside it (017, then 023, 024, 025, 031).

## The cycle for one issue

1. **Dispatch.** Start a worker subagent (Claude Code `Agent` tool,
   general-purpose, `isolation: "worktree"`). Give it:
   - the worker brief, verbatim;
   - the issue number and file;
   - the branch to use, `rehaul/issueNNN`, created from the current `main`;
   - the issues already merged, with their merge commits;
   - any owner answers in the progress file that concern this issue.

   Mark the issue `in progress` in the progress file, with its branch.
2. **Receive the report.** The brief defines its format. If the worker
   stopped on a question, handle it as in "Stops".
3. **Review.** Start a separate reviewer subagent with a fresh context, not
   the worker. It edits nothing. Its task:
   - for every acceptance criterion of the issue, find the test or manual
     check that covers it, and judge whether it really tests that behaviour;
   - check the diff against the design sections and decisions the issue
     cites, and against `_docs/tests.md`, `_docs/python_rules.md` and
     `_docs/docstring_schema.md`;
   - run the full suite and `git diff --check` on the branch;
   - return the defects found, each with file:line and a severity.

   Mark the issue `in review`.
4. **Fix loop.** Send the defects back to the worker (`SendMessage`), or to
   a new worker on the same branch, then review again. After three rounds
   without a clean review, stop and ask the owner.
5. **Merge.** On `main`, run
   `git merge --no-ff rehaul/issueNNN -m "Merge issue NNN: <title>"`, ending
   the message with the repository's attribution trailer. Then run
   `scripts\test.cmd` and `git diff --check` on `main`. If either fails,
   undo the merge with `git reset --hard ORIG_HEAD` (allowed only for a
   merge commit made in this step and not yet built on), record the failure,
   and go back to step 4.
6. **Close the issue text.** Add the status line closed issues use to the
   top of the issue file:
   `> **Status (<date>):** Implemented. Merged in <commit>.`, followed by
   any manual checks still owed. Then:
   - for 016–019, move the file to `_issues/_closed/` once all of stage 0 is
     merged;
   - for 021–031 and 033, leave it in `_issues/` until issue 032's hygiene
     step moves it (issue 032, scope item 6).

   Commit.
7. **Record.** In the progress file, set the issue to `merged` with its
   merge commit, test counts and pending manual checks, and add a log line.
   Commit.
8. **Clean up.** Remove the worker's worktree and delete the merged branch.

## Releases

- **0.18, after stage 0.** When 016–019 are merged, prepare the release with
  the procedure issue 016 left in `scripts\` and `tools\`: bump with
  `scripts\bump.cmd 0.18.0`, then record it locally (for example
  `scripts\release.cmd "<notes>" --no-push`). **Never push, publish or upload
  anything.** Record in the progress file that the owner must push the
  branch and tag and publish the release assets. Stage 1 does not wait for
  publication.
- **1.0, issue 032.** Same procedure with `1.0.0`. It is prepared locally
  and published by the owner.

## Stops

When one of the cases below occurs, stop the affected issue and write the
question in the progress file under "Owner questions", with the options.
Other ready issues may continue.

- The design and issues do not settle a decision the work needs, or the
  work would contradict a decision D1–D46.
- An acceptance criterion cannot be met as written.
- Three review rounds do not converge.
- An action would push, publish, tag on a remote, or change the real
  machine outside temporary directories, `--sandbox` and throwaway test
  locations.
- **Before merging issue 033.** The owner must first:
  - re-run spike S1 from an elevated shell;
  - run spike S2 on a clean VM, with a real Edge download and Smart App
    Control in evaluation mode;
  - record both results.

  The details are in the job 07 report, "Summary verdict", condition 3, and
  the scripts are in `_complete/07-spikes/`. Implement and review 033 fully,
  then stop before step 5 and ask.

When every remaining issue is blocked, end the session with a summary of
the open questions and of what is merged.

## Resume

The progress file is the source of truth. When the controller starts, it
checks every issue that is `in progress` or `in review`:

- **The branch is gone:** set the issue back to ready.
- **The branch exists but its worker is not running:** start a new worker
  on the existing branch. Give it the brief, and tell it to continue the
  earlier work, reading `08-rehaul-logs/issueNNN.md` and the branch log
  first.

## Constraints

- **Workers write the product code.** The controller writes no product
  code.
- **Never push or force-push.** Never rewrite `main` beyond undoing a merge
  made in step 5 of the same cycle.
- **`main` stays green.** Every merge passes the full suite.
- **The design is the authority.**
  - Do not renumber issues, and do not change a decision D1–D46.
  - When implementation shows that design or issue text is wrong in a way
    the fix makes obvious, the worker corrects it on its branch and
    reports it.
- **Stage 0's decisions stand (D13).** 0.17 keeps working until issue 029
  switches the front end.
- **Downloads are untrusted.** Never execute a downloaded artifact. Tests
  use recorded fixtures (design §16).
- **Commit messages written by an agent** end with the repository's
  attribution trailer.

## Completion criteria

- Every issue 016–019 and 021–033 is `merged` in the progress file. Each
  acceptance criterion is covered by an automated test or by a listed
  manual check in `tests/manual_smoke.md`.
- The epic's acceptance criteria (issue 020) hold, and issue 032's hygiene
  step has moved issues 020–033 to `_issues/_closed/`.
- `scripts\test.cmd` and `git diff --check` pass on `main`.
- 0.18 and 1.0 are prepared locally (version bumped, `RELEASES.md` row,
  local tag) and wait for the owner to publish them.
- The progress file lists every manual check still owed by the owner,
  including spikes S1 and S2 and the Windows-only smoke checks the issues
  name.
- This job's status is set to Closed with the final commit, and this file,
  the brief, the progress file and the logs move to `_complete/`.
