# Job 09: Controller for issue 035 (one scope per run)

Status: Open, not started.

Design authority, in this order:
1. `_issues/issue035-one-scope-per-run.md`. Its §6.2 holds the owner's answers, and they are binding.
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
| `09-one-scope-controller.md` | nobody | these instructions |
| `09-one-scope-worker-brief.md` | nobody | the brief every worker receives, verbatim |
| `09-tasks/Txx-*.md` | nobody | one task file per task |
| `09-one-scope-progress.md` | **you only** | status of every task, test counts, owner questions, log |
| `09-logs/Txx.md` | the worker of that task | the worker's plan and notes |

## Before the first task

1. Read `AGENTS.md`, issue 035, this file, the worker brief and the progress file.
2. Run the full suite on `main`:
   ```
   $env:GUPKG_NO_PAUSE='1'; .\scripts\test.cmd
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
| T01 | `09-tasks/T01-remove-migration-code.md` | T00 | gupkg |
| T02 | `09-tasks/T02-portable-package-kind.md` | T01 | gupkg |
| T03 | `09-tasks/T03-registry-conversion.md` | T02 | **gupkg-registry** |
| T04 | `09-tasks/T04-settings-schema-4-and-roots.md` | T02 | gupkg |
| T05 | `09-tasks/T05-scope-and-mode.md` | T04 | gupkg |
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

1. **Dispatch.** Start one worker with the `Agent` tool (general-purpose, `isolation: "worktree"`; T03 differs, see
   "The registry repository (T03)"). Give it, in this order:
   - the full text of `09-one-scope-worker-brief.md`;
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

- **T00 has no owner stop.** The owner confirmed D47–D55 on 2026-10-28 (issue 035 §6.1, answers Q10–Q15). Add to the
  reviewer's checks: every row D47–D55 in `_docs/rehaul_design.md` §19 says what issue 035 §6.1 says, including the
  amendments, and the design adds no rule the issue does not state. If so, merge T00 and go on to T01. If the note had
  to differ anywhere, or the worker stopped with a design question, stop and ask the owner.
- **T02 and T03 belong together.** After T02 is merged into gupkg `main`, run T03 at once. T03 changes a different
  repository; follow "The registry repository (T03)" below instead of the normal cycle where they differ.
- **T13 and the registry.** T13 syncs registries in its test roots. Until the owner has pushed the converted registry,
  the registry on GitHub is the old one, and gupkg from T02 on rejects it. Tell the T13 worker to use the local
  `gupkg-registry` clone as the registry source of its test roots. If gupkg cannot use a local folder as a registry
  source, stop and ask the owner (push first, or another source).
- **T13 needs the owner.** It writes manual checks for the owner. Close the job only after the owner has run them, or has
  said to close without them.

## The registry repository (T03)

**What it is.** `gupkg-registry` in this workspace (`C:\Users\guraltsev\Documents\devel\gupkg\gupkg-registry`) is a
separate Git repository. gupkg's `.gitignore` excludes it, so it is not part of gupkg's history and **a gupkg worktree
does not contain it**. Its remote `github` (`github.com/guraltsev/gupkg-registry`) is what every installed gupkg syncs
from by default, so whatever is pushed there reaches every user. Its `main` can hold owner commits that are not on
GitHub yet. You never push, fetch or pull it, and you never touch its other branches or worktrees.

**Preflight.** Before you dispatch T03, run these and paste the output into the progress file under T03:
```
git -C gupkg-registry branch --show-current
git -C gupkg-registry status --short
git -C gupkg-registry log --oneline -1
git -C gupkg-registry log --oneline github/main..main
```
- The branch must be `main`, and `status --short` must print nothing. Otherwise stop and ask the owner to commit or
  stash their changes. Never commit, stash, discard or check out anything over them yourself.
- The last command lists owner commits that are not pushed yet. They will go to GitHub together with T03, so record
  them in the progress file for the owner.
- gupkg `main` must have T02 merged, because the worker validates the registry with that source tree.

**Dispatch.** Start the worker **without** `isolation: "worktree"`, in the main workspace, because it needs the real
`gupkg-registry` folder and gupkg `main` with T02. Besides the usual brief and task, tell it:
- to create `i035/T03` from the registry's `main` (`git -C gupkg-registry checkout -b i035/T03 main`) and to commit only
  there;
- not to change any file of the gupkg repository except its log `.agents/jobs/09-logs/T03.md`, and not to commit in
  the gupkg repository;
- not to switch the gupkg checkout away from `main`.
While the worker runs, you do not touch either repository.

**Review.** The reviewer gets the five checks of the normal cycle, adapted, plus a sixth:
1. the "Done when" commands of T03, run by the reviewer itself;
2. if the branch adds tests under `_tests`, at least two fail on the registry's `main` and pass on the branch;
3. the registry's tests and gupkg's validator pass on the branch, and
   `git -C gupkg-registry diff --check main...i035/T03` is clean;
4. scope: `git -C gupkg-registry diff --stat main...i035/T03` touches only package folders, `_registry.toml`,
   `PORTABLE-UNVERIFIED.md`, launchers and `_tools/convert_035.py`. In gupkg, `git status --short` shows only the log;
5. no `download_url`, `sha256` or tracker setting changed: `git -C gupkg-registry diff main...i035/T03` filtered for
   those keys shows only lines of the new `-portable` copies;
6. **compatibility with the gupkg users run today.** Users on gupkg 1.0.8 will sync the converted registry once it is
   pushed. Check out the tag in a temporary worktree outside the workspace (`git worktree add <temp>\gupkg-v1.0.8
   v1.0.8`), validate the branch's registry with that tree's validator, paste the summary, and then remove the worktree
   (`git worktree remove`). A failure here does not block the merge. Record it under "Owner questions" as "pushing the
   converted registry breaks gupkg 1.0.8 for: <packages>", because it decides when the owner may push.

**Merge.** In the registry:
```
git -C gupkg-registry checkout main
git -C gupkg-registry merge --no-ff i035/T03 -m "Issue 035 T03: convert every package to the 035 rules"
```
The message ends with the attribution trailer. Then run the registry's tests and gupkg's validator on the registry's
`main` again. If either fails, undo only this merge (`git -C gupkg-registry reset --hard ORIG_HEAD`), record why, and go
back to the fix loop.

**Record.** In the progress file, write the merge commit as `registry <hash>`, the validator summary, and the result of
check 6. Add under "Owner actions pending":
- push `gupkg-registry` `main` to `github`. It carries T03 and the unpushed owner commits listed in the preflight;
- push it **before** releasing any gupkg built from this job, since that gupkg rejects the old registry. Whether the push
  may come earlier depends on check 6;
- after the push, run `gupkg registry sync` in each mode.

Then commit, in the gupkg repository, the progress file and `.agents/jobs/09-logs/T03.md`.

**Clean up.** `git -C gupkg-registry branch -d i035/T03`. Nothing else in the registry.

**No other task touches the registry.** If a later task would need to change it, stop and ask the owner.

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
