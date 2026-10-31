# Job 10: Controller for issue 036 (test suite quality)

Status: Open, not started.

Design authority, in this order:

1. `_issues/issue036-test-suite-quality.md`. Its §4 holds the owner's decisions, and they are binding.
2. `_docs/tests.md` and `_docs/tests_hygiene.md`.
3. `AGENTS.md`. Its working rules are mandatory.

## What this job does

It rebuilds the gupkg test suite without losing any protection:

- **tiers**;
- **the automatic hygiene checks**, with a baseline of today's violations that only shrinks;
- **one area at a time:** duplicates, history tests and tests of internals are removed, and the files move into
  `tests/<area>/`;
- **the registry data** moves to the registry;
- **the untested dangerous workflows** get tests.

The job is mostly deletion and rewriting of tests. Product code changes only where a task says so.

## What you do

You run the job. You do **not** write the tests or the code. For each task:

1. start a worker;
2. have a separate reviewer check the work;
3. merge it;
4. record it.

Do the tasks **one at a time, in the order of the table below**. Never run two workers at once.

## Files

| File | Who edits it | What it is |
| --- | --- | --- |
| `10-test-hygiene-controller.md` | nobody | these instructions |
| `10-test-hygiene-worker-brief.md` | nobody | the brief every worker receives, verbatim |
| `10-tasks/Txx-*.md` | nobody | one task file per task |
| `10-test-hygiene-progress.md` | **you only** | status of every task, indicators, owner questions, log |
| `10-logs/Txx.md` | the worker of that task | the worker's plan, the behaviour map, notes |

## Before the first task

1. Read `AGENTS.md`, issue 036, both policy files, this file, the worker brief and the progress file.
2. Run the full suite on `main`:

   ```text
   $env:GUPKG_NO_PAUSE='1'; .\scripts\test.cmd
   ```

   Write the counts and the wall time under "Baseline" in the progress file. If anything fails, stop and report it to the
   owner.
3. If the progress file shows a task `in progress` or `in review`, follow "Resume" first.

## Order

| Task | File | Depends on | Repository |
| --- | --- | --- | --- |
| T00 | `10-tasks/T00-indicators-tool.md` | — | gupkg |
| T01 | `10-tasks/T01-hygiene-checks-and-baseline.md` | T00 | gupkg |
| T02 | `10-tasks/T02-tiers.md` | T01 | gupkg |
| T03 | `10-tasks/T03-real-waits-and-big-copies.md` | T02 | gupkg |
| T04 | `10-tasks/T04-shared-support.md` | T03 | gupkg |
| T05 | `10-tasks/T05-registry-receives-its-tests.md` | T04 | **gupkg-registry** |
| T06 | `10-tasks/T06-gupkg-drops-registry-data.md` | T05 | gupkg |
| T07 | `10-tasks/T07-area-engine.md` | T06 | gupkg |
| T08 | `10-tasks/T08-area-catalog.md` | T07 | gupkg |
| T09 | `10-tasks/T09-area-cli.md` | T08 | gupkg |
| T10 | `10-tasks/T10-area-ui.md` | T09 | gupkg |
| T11 | `10-tasks/T11-area-report.md` | T10 | gupkg |
| T12 | `10-tasks/T12-area-services-network.md` | T11 | gupkg |
| T13 | `10-tasks/T13-area-services-unpack-and-hooks.md` | T12 | gupkg |
| T14 | `10-tasks/T14-area-system-and-self.md` | T13 | gupkg |
| T15 | `10-tasks/T15-area-self-runtime-and-project.md` | T14 | gupkg |
| T16 | `10-tasks/T16-area-model-and-registry.md` | T15 | gupkg |
| T17 | `10-tasks/T17-area-project-docs.md` | T16 | gupkg |
| T18 | `10-tasks/T18-unused-manager-policy.md` | T10 | gupkg (**owner approval first**) |
| T19 | `10-tasks/T19-gaps.md` | T17 | gupkg |
| T20 | `10-tasks/T20-acceptance.md` | T00–T19 | gupkg |

A task is **ready** when every task it depends on is `merged`. Even when two tasks are ready, take the lower number first.

## The cycle for one task

1. **Dispatch.** Start one worker with the `Agent` tool (general-purpose, `isolation: "worktree"`; T05 differs, see "The
   registry repository (T05)"). Give it, in this order:
   - the full text of `10-test-hygiene-worker-brief.md`;
   - the full text of the task file;
   - the branch name `i036/Txx`;
   - the list of tasks already merged, with their merge commits;
   - any owner answers from the progress file that concern this task.

   Set the task to `in progress` in the progress file, with its branch.
2. **Read the report.** The brief defines its format. If the worker stopped on a question, go to "Stops".
3. **Review.** Start a **different** agent with a fresh context. It edits nothing. Give it the task file, issue 036, both
   policy files, the branch name, and these checks:
   1. Every item in the task's "Done when" list holds. Run each command listed there yourself.
   2. **No protection was lost.**
      - Take the behaviour map in `10-logs/Txx.md`.
      - Pick at least eight removed or merged tests at random (all of them if fewer). For each, read the old test on
        `main`, find the test that the map names as its new owner, and confirm that the new one fails for the same
        breakage.
      - For at least two of them, prove it: break the product code the old test protected, in a scratch worktree, and
        show that the new owner test fails.
   3. **Every new test passes the test of a test** (`tests.md` §2.1, Q1–Q6). For at least two new tests, copy only the
      test onto `main` (or revert the protected code) and see it fail.
   4. **The suite is green and no worse:**
      - `scripts\test.cmd --all` passes on the branch;
      - `git diff --check main...i036/Txx` is clean;
      - the hygiene baseline has no added line (`git diff main...i036/Txx -- tests/hygiene_baseline.txt` shows only
        removals).
   5. **Scope.** The change stays inside the task's scope. Name any file it changes that the task does not mention, and
      any product (`src/`) change the task does not allow.
   6. **Machine safety.** Nothing touched the real machine. Search the diff and the logs for `C:\opt`,
      `C:\Users\guraltsev\opt`, `C:\bin` and `%USERPROFILE%\bin` used outside test strings.

   The reviewer returns a list of defects, each with `file:line`, a severity (blocking / minor) and what is wrong. Set the
   task to `in review`.
4. **Fix loop.** Send the blocking defects back to the worker with `SendMessage`, or to a new worker on the same branch
   with the brief and the task. Then review again. After **three** review rounds that still find blocking defects, stop
   and ask the owner.
5. **Merge.** On `main`:

   ```text
   git merge --no-ff i036/Txx -m "Issue 036 Txx: <task title>"
   ```

   The message ends with the repository's attribution trailer. Then run `scripts\test.cmd --all` and
   `git diff --check HEAD~1`. If either fails, undo with `git reset --hard ORIG_HEAD` (only for the merge you just made),
   record why, and go back to step 4.
6. **Record.** In the progress file:
   - set the task to `merged`;
   - write the merge commit;
   - record the indicators from `tools\test_report.py` (after T00), the test counts per tier, the baseline line count and
     any manual checks owed;
   - add a log line.

   Commit the progress file.
7. **Clean up.** Remove the worktree and delete the merged branch.

## The registry repository (T05)

`gupkg-registry` is a separate Git repository inside this workspace. gupkg's `.gitignore` excludes it, so a gupkg worktree
does not contain it. Its remote `github` is what every installed gupkg syncs from.

- **Never** push, fetch or pull it, and never touch its other branches.
- **Preflight:** before dispatching T05, run these and paste the output into the progress file:

  ```text
  git -C gupkg-registry branch --show-current
  git -C gupkg-registry status --short
  git -C gupkg-registry log --oneline github/main..main
  ```

  The branch must be `main`, and `status` must be empty. Otherwise stop and ask the owner. Never commit, stash or
  discard their changes.
- **Dispatch:** run the worker **without** `isolation: "worktree"`, in the main workspace. It creates
  `git -C gupkg-registry checkout -b i036/T05 main` and commits only there. In gupkg it changes nothing except its log.
- **Review:** as usual, plus two checks:
  - the registry's tests pass on the branch, both against gupkg `main` and against the gupkg branch of T06 if it exists;
  - the registry's CI file (`.github/workflows/validate.yml`) runs the moved tests from the registry checkout.
- **Merge** in the registry, with `git -C gupkg-registry merge --no-ff i036/T05`.
- **Record** the merge commit as `registry <hash>`, and add under "Owner actions pending": push `gupkg-registry` `main`
  **before** T06 is merged into gupkg `main`. The registry's CI runs gupkg modules from gupkg `main`, and T06 removes
  them.

**T06 waits** until the owner confirms the push. Record that wait as `blocked` with the reason.

## Stops

Stop the task, and write a question under "Owner questions" in the progress file with the options, when:

- issue 036 and the policy files do not settle a decision the work needs;
- a behaviour has no clear owner test, and keeping it would mean keeping a test that breaks the policy;
- a task would change product code it does not name;
- three review rounds do not converge;
- a worker reports that it failed twice at the same problem (AGENTS rule 10);
- any step would push, tag, publish, or change the real machine.

When the next task is blocked, end the session with a summary: what is merged, what is blocked, and the questions.

## Resume

The progress file is the source of truth. For each task that is `in progress` or `in review`:

- **The branch is gone:** set the task back to `ready`.
- **The branch exists, but no worker is running:** start a new worker on the existing branch, with the brief and the task.
  Tell it to read `10-logs/Txx.md` and `git log main..i036/Txx` first and to continue from there.

## Rules you follow

- Never push or force-push. Never rewrite `main` except to undo the merge you made in step 5 of the same cycle.
- `main` stays green: every merge passes `scripts\test.cmd --all`.
- Never touch `C:\opt`, `C:\Users\guraltsev\opt`, `C:\bin`, `%USERPROFILE%\bin`, the real Start Menu or the real `PATH`.
- Do not release and do not bump the version.
- Commit messages written by an agent end with the repository's attribution trailer.
