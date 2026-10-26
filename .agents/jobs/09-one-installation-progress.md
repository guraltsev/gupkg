# Job 09 progress

The controller's state for job 09 (`09-one-installation-controller.md`). Only the controller edits this file.

Statuses: `waiting` (dependencies not merged), `ready`, `in progress`, `in review`, `blocked` (see "Owner questions"),
`merged`.

## Baseline

Not recorded yet. Before T00, the controller records:

- the test counts of `scripts\test.cmd` on `main`;
- the source and test line counts.

## Tasks

| Task | Title | Depends on | Status | Branch | Merge commit | Tests | Manual checks pending |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T00 | Design note | — | ready | | | | |
| T01 | Remove migration code | T00 + owner OK | waiting | | | | |
| T02 | Portable is a package kind; side restriction | T01 | waiting | | | | |
| T03 | Registry conversion (gupkg-registry) | T02 | waiting | | | | |
| T04 | Settings schema 4 and root fallback chain | T02 | waiting | | | | |
| T05 | Installation object; side from the token | T04 | waiting | | | | |
| T06 | Catalog over one root | T05 | waiting | | | | |
| T07 | Remove the elevated relaunch | T06 | waiting | | | | |
| T08 | Run-as-administrator shortcut | T07 | waiting | | | | |
| T09 | Manager: one scope per run | T07 | waiting | | | | |
| T10 | Manager: shared reader, visible failures | T09 | waiting | | | | |
| T11 | Launchers: bundled, gupkg-location, PATH | T07 | waiting | | | | |
| T12 | Docs and consistency sweep | T01–T11 | waiting | | | | |
| T13 | Acceptance | T12 | waiting | | | | |

## Owner actions pending

- None yet.

## Owner questions

- None yet.

## Log

- 2026-10-26: job prepared from issue 035; not started.
