# Job 09 progress

The controller's state for job 09 (`09-one-scope-controller.md`). Only the controller edits this file.

Statuses: `waiting` (dependencies not merged), `ready`, `in progress`, `in review`, `blocked` (see "Owner questions"),
`merged`.

## Baseline

Not recorded yet. Before T00, the controller records:

- the test counts of `scripts\test.cmd` on `main`;
- the source and test line counts.

## Tasks

| Task | Title | Depends on | Status | Branch | Merge commit | Tests | Manual checks pending |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T00 | Design note | — | merged | i035/T00 | 7c62967 | 3915 passed, 0 failed, 11 skipped | none |
| T01 | Remove migration code | T00 | merged | i035/T01 | 91faed3 | 3765 passed, 0 failed, 11 skipped | none |
| T02 | Portable is a package kind; mode restriction | T01 | merged | i035/T02 | 30030c3 | 3720 passed, 0 failed, 11 skipped | 035-T02-1, 035-T02-2 (tests/manual_smoke.md) |
| T03 | Registry conversion (gupkg-registry) | T02 | in progress | i035/T03 (registry) | | | |
| T04 | Settings schema 4 and root fallback chain | T02 | waiting | | | | |
| T05 | Scope object; mode from the token | T04 | waiting | | | | |
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

- Non-blocking, from the T00 review (rulings needed by the task that touches them):
  1. Machine-interface `--json` results and `--plan` documents still carry a per-target `mode` and `requires_elevation`
     (design §10.4). Issue 035 §4.3 only says `list --json` carries `mode`/`root` once. Needed by T07 (api 2). Suggest: drop
     per-target `mode` and `requires_elevation`; put `mode` and `root` once at document level.
  2. Bootstrap `-Scope` to `--mode` mapping (design §21.5, `gupkg-bootstrap.ps1`) has no replacement in the issue. Design says
     "the mode is that of the process". Needed by T11/T12.
  3. T00 chose `scope.py` as the module name, and mapped D17 to D47, D22 to D53, D37 to D49 as superseding decisions; the issue
     names none of these. Treated as working choices, not owner decisions.
  4. Design §21.3 says only installing the `gupkg` package writes `gupkg-location` and bundled installs never do; the issue
     says only the first half.
  5. Leftover stale text in the design (worker's list of 9 plus design line ~1130, D19 row "relaunching itself", §7.3, §7.5,
     §9.1, §9.3, §13.1, §13.4, §17, §20, §21.5). Sweep in T12.

- Non-blocking, from T01: `engine/executor.py` ~795 `drop_receipts` still unlinks `.gupkgeceipts\<v>.toml`; dead after T01 (cleanup text still says "receipts"). Out of T01's scope; T12 sweep or later.

- Non-blocking, from T02 review: `engine/planner.py:192-193` secondary guard still says "does not support {mode} mode (it supports ...)" instead of the D55 refusal constants; `engine/lifecycle.py:660` `_ask_about_other_versions` wording odd for portable; the system-only refusal carries the literal `gupkg <command> <name>` (check_supported does not know them). Decide in T05/T07.

## T03 preflight (2026-10-29)

```
git -C gupkg-registry branch --show-current   -> main
git -C gupkg-registry status --short          -> (empty)
git -C gupkg-registry log --oneline -1        -> 9fba5c5 volta: shim only volta and put App on the PATH
git -C gupkg-registry log --oneline github/main..main -> 9fba5c5 volta: shim only volta and put App on the PATH
```

Owner commit not pushed yet (goes to GitHub together with T03): `9fba5c5 volta: shim only volta and put App on the PATH`.

## Log

- 2026-10-26: job prepared from issue 035; not started.

- 2026-10-28: baseline recorded (3915 passed, 11 skipped; src 37739, tests 40938 lines). T00 dispatched on `i035/T00`.
- 2026-10-29: T00 worker reported (1df03dc on i035/T00); review started.
- 2026-10-29: T00 reviewed (0 blocking, 8 minor), merged as 7c62967; suite 3915 passed, 11 skipped. T01 ready.
- 2026-10-29: T01 dispatched on i035/T01.
- 2026-10-29: T01 worker reported (6c83a4f, 3765 passed/11 skipped); review round 1 started.
- 2026-10-29: T01 round 1 (6 comment defects) fixed in 656ae44; merged as 91faed3; suite 3765 passed, 11 skipped (150 fewer tests: deleted migration/legacy tests). T02 ready.
- 2026-10-29: T02 dispatched on i035/T02.
- 2026-10-29: T02 worker reported (10 commits, 3720 passed per worker; last test edit not re-run in full); review round 1 started.
- 2026-10-29: T02 reviewed (0 blocking, 4 minor), merged as 30030c3; suite 3720 passed, 11 skipped. T03 preflight clean; T03 dispatched in the main workspace.
