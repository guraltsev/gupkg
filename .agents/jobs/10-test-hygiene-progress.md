# Job 10 progress

The controller's state for job 10 (`10-test-hygiene-controller.md`). Only the controller edits this file.

Status: Open, not started.

Statuses: `waiting` (dependencies not merged), `ready`, `in progress`, `in review`, `blocked` (see "Owner questions"),
`merged`.

## Baseline

Not recorded yet. Before T00, the controller records the test counts and the wall time of `scripts\test.cmd` on `main`.
After T00, it records `tools\test_report.py --timings`.

For reference, issue 036 §2 measured on 2026-10-30:

- 3,770 tests (3,757 passed, 13 skipped), 7 min 14 s serially;
- about 438k tokens of test code, against 405k of source.

## Tasks

| Task | Title | Depends on | Status | Branch | Merge commit | Tests (per tier) | Baseline lines | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T00 | Indicators tool | — | ready | | | | | |
| T01 | Hygiene checks and baseline | T00 | waiting | | | | | |
| T02 | Tiers | T01 | waiting | | | | | rule 3 wording needs the owner |
| T03 | Real waits and big copies | T02 | waiting | | | | | |
| T04 | Shared support | T03 | waiting | | | | | |
| T05 | Registry receives its tests (gupkg-registry) | T04 | waiting | | | | | |
| T06 | gupkg drops registry data | T05 | waiting | | | | | waits for the owner's push of T05 |
| T07 | Area engine | T06 | waiting | | | | | |
| T08 | Area catalog (with scope and settings) | T07 | waiting | | | | | |
| T09 | Area cli | T08 | waiting | | | | | |
| T10 | Area ui (with tui and manager) | T09 | waiting | | | | | |
| T11 | Area report | T10 | waiting | | | | | |
| T12 | Area services: network and checksums | T11 | waiting | | | | | |
| T13 | Area services: unpack and hooks | T12 | waiting | | | | | |
| T14 | Areas system and self (launchers) | T13 | waiting | | | | | |
| T15 | Areas self (runtime, self-update) and project | T14 | waiting | | | | | |
| T16 | Areas model and registry | T15 | waiting | | | | | |
| T17 | Area project (docs), last flat files | T16 | waiting | | | | | |
| T18 | Unused manager_policy code | T10 | waiting | | | | | owner approval first |
| T19 | Gaps | T17 | waiting | | | | | |
| T20 | Acceptance | T00–T19 | waiting | | | | | |

## Owner questions

- **T02:** the wording of `AGENTS.md` rule 3. The worker proposes it, and the owner approves it.
- **T18:** may the unused `decide_targets` and `command_line` in `gupkg.model.manager_policy` be removed?

## Owner actions pending

None yet.

## Log

- 2026-10-30: job written. Not started.
