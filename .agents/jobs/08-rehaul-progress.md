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
| 019 | The bundled Python ships without pip | 016 | merged | rehaul/issue019 | 5c922cb | 331 passed, 0 failed, 0 skipped | 019-1 real build; 019-2 clean-VM pip-less run; 019-3 update_deps --repair no-diff |
| 021 | System boundary, FakeSystem, and sandbox mode | 016–019 | merged | rehaul/issue021 | 2fcf02c | 419 passed, 0 failed, 0 skipped | 021-1 sandbox vs real HKCU/Start Menu; 021-2 WM_SETTINGCHANGE; 021-3 UAC relaunch; 021-4 admin detection and --scope auto |
| 022 | Event stream, console renderer, and standard logs | 021 | in progress | rehaul/issue022 | | | |
| 023 | `pkg.toml` format 2, settings model, and migration tool | 019 (and D35) | merged (part B pending, see notes) | rehaul/issue023 | 8e384cd | 642 passed, 0 failed, 0 skipped | 023-1 launchers refuse elevated; 023-2 elevated settings layers; 023-3 run tool on a copy of a real 0.17 C:/opt |
| 024 | Catalog and package state model | 023 | in progress | rehaul/issue024 | | | |
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

- **0.18:** prepared locally 2026-10-22. Version bumped (0a4a63f), tag `v0.18.0` on 0a4a63f, `RELEASES.md` row committed in the "Release 0.18.0" commit, ZIP `dist/gupkg-0.18.0-win-amd64.zip` (sha256 `240d4ace40059a6ca65d85241459881f7692aa4029ff084408f16823f437d124`, built from a real fetch with pinned digests; the build probe passed). **Nothing was pushed, published or uploaded.** The owner must: push the branch and tag (`git push github main` and `git push github v0.18.0`; the tool printed `origin`, but this repo's remote is `github`), then create the GitHub release `v0.18.0` with the ZIP as asset (`gh release create v0.18.0 dist\gupkg-0.18.0-win-amd64.zip --title "gupkg 0.18.0" --notes ...`). The `RELEASES.md` row's digest must stay equal to the uploaded bytes: upload this exact file. The ZIP was built with `tools/build_standalone.py --python <base Python with pip>` because the repo `.venv` is uv-made and has no pip (see Owner questions); the real-network build is otherwise manual check 019-1.
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

None blocking. For information:

- The repo `.venv` was created by `uv` and has no pip, but 018/019 made the build tools (`scripts/build-standalone.cmd`, `release.cmd`, `update_deps.py`) use the `.venv`'s pip. `scripts/release.cmd` therefore fails with "No module named pip" until the `.venv` is recreated with `python -m venv` and `pip install -r requirements-dev.txt` (the docs now say so). The controller worked around it for 0.18 with `--python`. 1.0 will need the same unless the `.venv` is recreated.

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
- 2026-10-22: 019 merged (5c922cb) after 1 review round + small fixes. Suite on main: 331 passed. Stale ignored `pip/` bytecode directory in the main checkout's committed runtime caused 3 spurious failures right after the merge; removed (local state only, not tracked).
- 2026-10-22: 018 review round 3 clean (round 1: sys.path/ctypes ahead of ACL check + fail-open is_elevated; round 2: code beside interpreter ran via `_pth`/`site` before the check; fixed with `-S` and a minimal `_pth`). 018 now merges main (conflicts with 019 expected in operations.md, manual_smoke.md, requirements-dev.txt, `_pth`/fetch_runtime/test_runtime_tools, test_launcher).
- Owner note (D13 text): worker 018 edited `_docs/rehaul_design.md` D13 and the 018 row to say the launcher flags are `-I -s -B -S` (the added `-S`). It is a text fix for an implementation consequence, not a change of the decision; please confirm. Also: the elevated ACL check is not recursive over the runtime tree (D30 wants recursion) and the python dir is trusted at interpreter start (inherent limit); recursion is routed to issue 033, and the limit should be stated in the elevated-runtime doc (issue 032).
- 2026-10-22: 018 merged (1f4e085) after 3 review rounds (round 3 clean) and a merge of main (019). Suite on main: 355 passed. Stage 0 (016-019) is fully merged.
- Stale text noted by the 018 worker: `docs/elevated-runtime-design.md` "Dependencies ship in the bundle" bullet still says the lock pins pip and that update/repair run pip from the bundled runtime (stale after 019); fix in issue 032's docs pass.
- 2026-10-22: 0.18 prepared locally (see Releases). Dispatched workers for 021 and 023 (stage 1).
- 2026-10-22: 023 worker done (4 commits, 553 passed); reviewer started. Worker deferred 'adoption of 0.17 installs' (reviewer to judge).
- 2026-10-22: 021 worker done (3 commits, 408 passed); reviewer started (incl. trial merge with 023 branch and 018 security-regression check).
- 2026-10-22: 023 review round 1 (no blockers; major: migration tool turns non-artifact download URLs into `shipped = true` and drops checksums silently; minors: defines_track misses except/match bodies, settings read-before-trust-check, non-transactional apply). Sent back. 021 review round 1 (018 security guarantees verified; major: sandbox `run` delegates unknown programs to the real system; minors). Sent back. Merge order: 021 first, then 023 ports its imports (settingsload, gupkg_migrate/shortcuts, 2 tests) onto the System boundary.
- Scope gaps found by the 023 reviewer, for 028's dispatch (controller decision, flagged for the owner): (a) "adoption of 0.17 installs" (install record from `current` + declared components, reading and removing `update.toml` and old receipts, `gupkg list` unchanged, `uninstall` removes old shortcuts) is deferred by issue 023 until 024 and 028 land, and no later issue builds it: 028 item 10 says it is done by the migration tool; I will include it ("023 part B") in the 028 worker's assignment, in `gupkg_migrate`. (b) Issue 023 scope item 9 says `configuration.py` leaves core, but 028 item 11 does not list `configuration.py`, `configfix.py`, the 0.17 parts of `metadata.py` or the 0.17 `*_to_gupkg_toml.py` scripts: I will add them to the 028 worker's deletion list. Reviewer also suggests deleting `module` from the 023 scope-item-2 spelling list (stale; `script` is the §4/D27 spelling) and having 033 make the migration tool use its launcher template.
- 2026-10-22: 021 merged (2fcf02c) after 3 review rounds (r1: sandbox `run` delegated unknown programs to the real system; r2: allow-list matched by file name (`git.cmd` ran); r3 clean). Suite on main: 419 passed. 018's pre-import guarantees verified unchanged by reviewers (elevation code moved to `gupkg/selfmgmt/elevation.py`, still loaded by file path before `sys.path` changes). Dispatched 022. 023 is to merge main and port its imports onto the System boundary.
- Notes carried from 021 for later issues: (026/028) with the sandbox allow-list (git and 7-Zip by identity), any package-local program/installer/script hits `UnexpectedCall`, so 028 must route every external process through `System.run` and let `pkg check --install` declare per-package doubles or an opt-in allow-list, or report "needs a real run". Minor 021 follow-ups (non-blocking): `fake.py _find_on_path` accepts relative PATH entries such as `.` (docstring says never the cwd), drive-relative `C:git` is treated as bare `git`, `WindowsSystem.run` timeout kills only the direct child (not grandchildren), hook context gained `fetch_text` while `PKG_MODULE_API` stays 1 (design §8.6 makes it 2 later), `is_elevated` is now one fail-closed implementation in `selfmgmt/elevation.py`.
- 2026-10-22: 023 merged (8e384cd) after 2 review rounds (r1: migration tool dropped download URLs/checksums, non-transactional apply, settings read-before-trust-check, defines_track gaps; r2 clean). Suite on main: 642 passed. Part B (adoption of 0.17 installs) is NOT done: it is assigned to the 028 worker. Dispatched 024.
- Minor 023 follow-ups recorded by the reviewer (non-blocking; for the 028 worker's migration-tool part B or a later cleanup): in `_convert_version` an own `[[origin.versions]]` entry is dropped silently when the inline origin url is a project page; `apply_plan` rollback does not restore mtimes, leaves an empty `pkg.local`, may leave a read-only `.bak`; the final unlink of original scripts is outside the transaction and `_run_package` does not catch OSError/ValueError (one bad directory aborts a registry run); `defines_track` misses exotic bindings (`for track in`, `with ... as track`, walrus, starred, match capture, `type track`), bare `track: int` counts as defined; `SHA256:<hex>` prefix is case-sensitive; no tests for the System-raises / non-None paths in settingsload; `read_layer` uses `folder.exists()` on the real filesystem so FakeSystem untrusted_dirs for non-existent folders are not consulted; the "comments are not preserved; .bak keeps them" note is misleading with `--no-backup`; unused imports (`field` in pkgcheck.py, `Any` in gupkg_migrate/shortcuts.py); `model/version.py` comments name 0.17/_bootstrap (D33 wording).
