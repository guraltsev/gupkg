# Job 08 progress

The controller's state for job 08 (`08-rehaul-controller.md`). Only the
controller edits this file.

Statuses: `waiting` (dependencies not merged), `ready`, `in progress`,
`in review`, `blocked` (see "Owner questions"), `merged`.

## Baseline

Not recorded yet. The controller records the result of `scripts\test.cmd`
on `main` before dispatching the first worker.

## Issues

| Issue | Title | Depends on | Status | Branch | Merge commit | Tests | Manual checks pending |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 016 | One self-contained release ZIP | — | merged | rehaul/issue016 | 07d20ba | 296 passed (+39 incl. 017), 0 failed, 0 skipped | 016-1..016-5 in tests/manual_smoke.md (VM user/system bootstrap, self update, edited-file status, release tool on a throwaway fork) |
| 017 | Shortcuts and junctions without PowerShell or `cmd` | — | merged | rehaul/issue017 | e23a94c | 277 passed (+20), 0 failed, 0 skipped | 017-1 Explorer Properties dialog; 017-2 TUI and elevated system-scope install; 017-3 optional timing |
| 018 | Remove system-Python autodetection | 016 | in review | rehaul/issue018 | | | |
| 019 | The bundled Python ships without pip | 016 | in review | rehaul/issue019 | | | |
| 021 | System boundary, FakeSystem, and sandbox mode | 016–019 | waiting | | | | |
| 022 | Event stream, console renderer, and standard logs | 021 | waiting | | | | |
| 023 | `pkg.toml` format 2, settings model, and migration tool | 019 (and D35) | waiting | | | | |
| 024 | Catalog and package state model | 023 | waiting | | | | |
| 025 | First-class install modes | 021, 023 | waiting | | | | |
| 026 | Built-in services replacing hook scripts | 021, 022, 023 | waiting | | | | |
| 027 | Checksum policy | 023, 026 | waiting | | | | |
| 028 | Execution engine | 021, 022, 024, 025, 026, 027 | waiting | | | | |
| 029 | Unified command-line interface and machine interface | 028 | waiting | | | | |
| 030 | `gupkg-tui` and `gupkg-manager` | 018, 022, 027, 029 | waiting | | | | |
| 031 | Registry 2 | 023, 026, 027 | waiting | | | | |
| 033 | Bundled mode, package launchers, and bootstrap | 016, 023, 026, 028, 029, 030 | waiting | | | | |
| 032 | Migration path, documentation, issue hygiene, 1.0 release | all | waiting | | | | |

## Releases

- **0.18:** not prepared (after 016–019 are merged).
- **1.0:** not prepared (issue 032).

## Owner checks pending

- **Spike S1, elevated re-run.** Run
  `.agents/jobs/_complete/07-spikes/s1/run-s1.ps1` from an elevated shell
  and record the result. It gates merging issue 033.
- **Spike S2, clean VM.** Download a distribution ZIP with Edge, with Smart
  App Control in evaluation mode, and run
  `.agents/jobs/_complete/07-spikes/s2/run-s2.ps1`. Record the result. It
  gates merging issue 033.

## Owner questions

None.

## Log

- 2026-10-22: job prepared; not started.
- 2026-10-22: baseline recorded (257 passed). Dispatched workers for 016 and 017.
- 2026-10-22: 017 worker done (d09427f, 271 passed); reviewer started.
- 2026-10-22: 017 merged (e23a94c) after 2 review rounds (round 1: HRESULT overflow, wrong RPC_E_CHANGED_MODE constant; round 2 clean). Suite on main: 277 passed. Issue file moves to `_issues/_closed/` when all of stage 0 is merged.
- 2026-10-22: 016 worker done (4 commits, 271 passed on its branch); reviewer started (also trial-merge vs main with 017).
- 2026-10-22: 016 merged (07d20ba) after 2 review rounds (round 1: blocking .pyc-in-manifest build defect, __pycache__ exemption hole; round 2 clean). Suite on main: 296 passed. Dispatched workers for 018 and 019.
- Known minor follow-ups from 016 review (non-blocking, for 018/019/033/032 to pick up): (m1) bootstrap reuse check compares an existing folder with its own manifest, not the verified payload's manifest (issue 033 reworks the bootstrap); (m2) `integrity.py` does not list planted junctions/symlinks as extra (elevated path also runs the ACL trust check).
- 2026-10-22: 018 worker done (9ec691d, 306 passed); reviewer started.
- 2026-10-22: 018 review round 1: 2 major security findings (sys.path/ctypes before ACL check; is_elevated fails open) sent back to worker. 019 worker done (b64a120, f4ef00a; 330 passed); reviewer started.
