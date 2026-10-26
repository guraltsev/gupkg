# Job 09: Controller for issue 035 (one installation per run)

Status: Open, not started.

Design authority, in this order:
1. `_issues/issue035-one-installation-per-run.md`. Its §6.2 holds the owner's answers, and they are binding.
2. `_docs/rehaul_design.md`, after task T00 has updated it.
3. `AGENTS.md`. Its working rules are mandatory.

## What you do

You run the job. You do **not** write product code. For each task:

1. start a worker;
2. have a separate reviewer check the work;
3. merge it;
4. record it.

You do the tasks **one at a time, in the order of the table below**. Never run two workers at once.

## Files

| File | Who edits it | What it is |
| --- | --- | --- |
| `09-one-installation-controller.md` | nobody | these instructions |
| `09-one-installation-worker-brief.md` | nobody | the brief every worker receives, verbatim |
| `09-tasks/Txx-*.md` | nobody | one task file per task |
| `09-one-installation-progress.md` | **you only** | status of every task, test counts, owner questions, log |
| `09-logs/Txx.md` | the worker of that task | the worker's plan and notes |

## Before the first task

1. Read `AGENTS.md`, issue 035, this file, the worker brief and the progress file.
2. Run the full suite on `main`:
   ```
   $env:GUPKG_NO_PAUSE='1'; .\scripts\test.cmd -q
   ```
   Write the counts (passed, failed, skipped) under "Baseline" in the progress file. If anything fails, stop and report
   it to the owner. Do not start T00.
3. Write down the line counts that the issue will compare against. Put the output under "Baseline" in the progress file:
   ```
   (git ls-files src | Where-Object { $_ -like '*.py' -and $_ -notlike '*python/Lib*' } | ForEach-Object { (Get-Content $_).Count } | Measure-Object -Sum).Sum
   (git ls-files tests | Where-Object { $_ -like '*.py' } | ForEach-Object { (Get-Content $_).Count } | Measure-Object -Sum).Sum
   ```
4. If the progress file shows a task `in progress` or `in review`, follow "Resume" first.

## Order

| Task | File | Depends on | Repository |
| --- | --- | --- | --- |
| T00 | `09-tasks/T00-design-note.md` | — | gupkg |
| T01 | `09-tasks/T01-remove-migration-code.md` | T00 + owner OK | gupkg |
| T02 | `09-tasks/T02-portable-package-kind.md` | T01 | gupkg |
| T03 | `09-tasks/T03-registry-conversion.md` | T02 | **gupkg-registry** |
| T04 | `09-tasks/T04-settings-schema-4-and-roots.md` | T02 | gupkg |
| T05 | `09-tasks/T05-installation-and-side.md` | T04 | gupkg |
| T06 | `09-tasks/T06-catalog-one-root.md` | T05 | gupkg |
| T07 | `09-tasks/T07-remove-relaunch.md` | T06 | gupkg |
| T08 | `09-tasks/T08-run-as-admin-shortcut.md` | T07 | gupkg |
| T09 | `09-tasks/T09-manager-one-scope.md` | T07 | gupkg |
| T10 | `09-tasks/T10-manager-reader-and-failures.md` | T09 | gupkg |
| T11 | `09-tasks/T11-launchers.md` | T07 | gupkg |
| T12 | `09-tasks/T12-docs-and-consistency.md` | T01–T11 | gupkg |
| T13 | `09-tasks/T13-acceptance.md` | T12 | gupkg |

A task is **ready** when every task it depends on is `merged`. Even when two tasks are ready, take the lower number first.

## The cycle for one task

1. **Dispatch.** Start one worker with the `Agent` tool (general-purpose, `isolation: "worktree"`). Give it, in this order:
   - the full text of `09-one-installation-worker-brief.md`;
   - the full text of the task file;
   - the branch name `i035/Txx` (for T03: `i035/T03` in the `gupkg-registry` repository);
   - the list of tasks already merged, with their merge commits;
   - any owner answers from the progress file that concern this task.

   Set the task to `in progress` in the progress file, with its branch.
2. **Read the report.** The brief defines its format. If the worker stopped on a question, go to "Stops".
3. **Review.** Start a **different** agent with a fresh context. It edits nothing. Give it:
   - the task file, issue 035 and the branch name;
   - this list of checks:
     1. every item in the task's "Done when" list holds, and the reviewer runs each command listed there itself;
     2. every new test fails on `main` and passes on the branch. The reviewer checks this for at least two tests: it
        copies only the test file onto `main` and runs it there;
     3. the full suite passes on the branch, and `git diff --check main...i035/Txx` is clean;
     4. the change stays inside the task's scope. Name any file changed that the task does not mention;
     5. nothing touched the real machine. Search the diff and the logs for `C:\opt`, `C:\Users\guraltsev\opt`,
        `C:\bin` and `%USERPROFILE%\bin` used outside of test strings;
   - the instruction to return a list of defects, each with `file:line`, a severity (blocking / minor), and what is
     wrong.

   Set the task to `in review`.
4. **Fix loop.** Send the blocking defects back to the worker with `SendMessage` (or to a new worker on the same branch
   with the brief and the task). Then review again. After **three** review rounds that still find blocking defects, stop
   and ask the owner.
5. **Merge.** On `main`:
   ```
   git merge --no-ff i035/Txx -m "Issue 035 Txx: <task title>"
   ```
   The message ends with the repository's attribution trailer. Then run the full suite and `git diff --check HEAD~1`. If
   either fails, undo with `git reset --hard ORIG_HEAD` (only for the merge you just made), record why, and go back to
   step 4.
6. **Record.** In the progress file:
   - set the task to `merged`;
   - write the merge commit, the test counts and any manual checks still owed;
   - add a log line.

   Commit the progress file.
7. **Clean up.** Remove the worktree and delete the merged branch.

## Special cases

- **T00 ends with an owner stop.** Merge T00 (it changes only documentation). Then write under "Owner questions": "Confirm
  D47–D55 as written in `_docs/rehaul_design.md` §6 and §19". Stop the session until the owner answers. Do not start
  T01 before then.
- **T02 and T03 belong together.**
  - After T02 is merged into gupkg `main`, run T03 at once.
  - T03 works in the `gupkg-registry` folder (a separate Git repository inside this workspace). Its branch is merged into
    the registry's `main`. **Never push either repository.**
  - Record in the progress file that the owner must push the registry after T03.
- **T13 needs the owner.** It writes manual checks for the owner. Close the job only after the owner has run them, or has
  said to close without them.

## Stops

Stop the task, and write a question under "Owner questions" in the progress file with the options, when:

- the issue and the design do not settle a decision the work needs;
- the work would contradict issue 035 §6 or a decision in `rehaul_design.md` §19;
- three review rounds do not converge;
- a worker reports that it failed twice at the same problem (AGENTS rule 10);
- any step would push, tag, publish, or change the real machine outside temporary folders, `--sandbox` and the test
  roots that T13 creates.

When the next task is blocked, end the session with a summary: what is merged, what is blocked, and the questions.

## Resume

The progress file is the source of truth. For each task that is `in progress` or `in review`:

- **The branch is gone:** set the task back to `ready`.
- **The branch exists, but no worker is running:** start a new worker on the existing branch. Give it the brief and the
  task, and tell it to read `09-logs/Txx.md` and `git log main..i035/Txx` first and to continue from there.

## Rules you follow

- Never push or force-push. Never rewrite `main` except to undo the merge you made in step 5 of the same cycle.
- `main` stays green. Every merge passes the full suite.
- Never touch `C:\opt`, `C:\Users\guraltsev\opt`, `C:\bin`, `%USERPROFILE%\bin`, the real Start Menu folders or the real
  `PATH` (issue 035 §6.2, Q3).
- Do not release and do not bump the version.
- Commit messages written by an agent end with the repository's attribution trailer.
