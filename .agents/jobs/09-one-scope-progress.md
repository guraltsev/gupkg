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
| T03 | Registry conversion (gupkg-registry) | T02 | merged | i035/T03 (registry) | registry da73597 | registry _tests 212 passed; pkg_check.py --all: 192 directories checked, no error; registry_tool validate: 95 packages, 97 pinned versions, 0 hook files, 1 unpinned: OK | none |
| T04 | Settings schema 4 and root fallback chain | T02 | merged | i035/T04 | 6ab01f8 | 3720 passed, 0 failed, 11 skipped | none | |
| T05 | Scope object; mode from the token | T04 | merged | i035/T05 | fc47455 | 3649 passed, 0 failed, 11 skipped (87 tests removed, 63 added; reasons in 09-logs/T05.md) | 035-T05-1..4 (tests/manual_smoke.md) |
| T06 | Catalog over one root | T05 | merged | i035/T06 | 955b8f0 | 3630 passed, 0 failed, 11 skipped (99 tests removed or merged, 9 added; reasons in 09-logs/T06.md); catalog.py 1042 -> 840 lines | 035-T06-1, 035-T06-2 (tests/manual_smoke.md) |
| T07 | Remove the elevated relaunch | T06 | in review | i035/T07 | | | |
| T08 | Run-as-administrator shortcut | T07 | waiting | | | | |
| T09 | Manager: one scope per run | T07 | waiting | | | | |
| T10 | Manager: shared reader, visible failures | T09 | waiting | | | | |
| T11 | Launchers: bundled, gupkg-location, PATH | T07 | waiting | | | | |
| T12 | Docs and consistency sweep | T01–T11 | waiting | | | | |
| T13 | Acceptance | T12 | waiting | | | | |

## Owner actions pending

- Push `gupkg-registry` `main` to `github`. It carries T03 (registry `da73597`, with the conversion, `f9f3441` volta fingerprint refresh and `cc24abf`) and the unpushed owner commit `9fba5c5` (volta).
- Push it **before** releasing any gupkg built from this job: gupkg from T02 on rejects the old registry. Check 6 (below) says gupkg 1.0.8 accepts the converted registry, so an earlier push is possible, with the consequence under "Owner questions".
- After the push, run `gupkg registry sync` in each mode.
- `f9f3441` refreshes the volta/2.0.2 fingerprint that owner commit `9fba5c5` left stale (`registry_tool validate` failed on `main` before). It is a separate commit the owner may drop, but then `validate` fails on volta again.

## Owner questions

- T03 check 6 (gupkg 1.0.8 against the converted registry): validated with the 1.0.8 tree's own validator: `pkg_check.py --all` 192 directories checked, no failing package; `registry_tool validate` 95 packages, OK. Nothing in the registry breaks 1.0.8's validator. Not run: a real install under 1.0.8. Consequence for users: under 1.0.8, the 14 split packages no longer offer a portable mode (users need `<name>-portable`), and `unins000.exe` is created by a plain `write` step.
- Non-blocking, from T03:
  1. The task and issue say "28 new `-portable` packages". There are **14** (28 `pkg.toml` files): copyq, crystaldiskinfo, crystaldiskmark, hwinfo, keepassxc, keypirinha, notepadplusplus, obsstudio, rufus, serviwin, systeminformer, vscode, winmerge, winscp. T13's expectations must say 14.
  2. `X` and `X-portable` declare identical shim and shortcut names (e.g. `keypirinha`/`Keypirinha`, `copyq`/`CopyQ`). Installed together in one root they collide. Owner decision: distinct names for the portable twins, or mutual exclusion?
  3. The copyq step is `write = { path = "unins000.exe", content = "" }` (the literal `["create", "unins000.exe"]` is not a verb of the plain `[[prepare]]`). Reviewer confirmed it creates the empty file; it now also runs in system mode, as the issue accepts.
  4. `gupkg-registry/README.md` line ~101 still mentions `[nonportable] remove` (stale; registry docs outside T03's list). `_tools/convert_035.py` is a one-shot tool left in `_tools`; owner may delete it.
- Non-blocking, from T04 review, handed to T05 (they are T05's to finish): `integrate = "none"` is silently ignored in system mode (`build_mode_context`); `gupkg_ui/settings.py` `checksum_policy(mode)` reads only the user-mode layers; `modes.py` "roots differ" check and stale wording (modes.py:25, :153, :222-229; model/settings.py:311 `default_layer()` docstring); `MODE_SYSTEM`/`MODE_USER` defined twice (model/settings.py, model/mode.py). `test_a_user_run_never_reads_the_system_root` is weak (uses the user root as `root=`); a variant with both `[roots]` keys set is wanted. README/_docs/operations.md still describe `default_mode`, `[integration.system|user]` (operations.md:67 `schema = 3`): T12.
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

- Non-blocking, from T05 (for T06/T07/T09/T12): `gupkg-bootstrap.ps1` lost `--mode $Scope` on its install lines (consequence of removing `--mode`; T11 reviews the script). `Scope.requires_elevation`, the one-element group loop and the mixed-modes text in `cli.py`/`lifecycle.py`, `engine/checkupdates.py` `writes_root` are dead until T07. `gupkg_manager/cli.py` still has its own `--mode` filter (T09). The UI two-group logic and `can_elevate` are untouched (T09); the UI `checksum_policy` guard for system targets is kept and marked `# Removed in issue 035 T09.` (it only turns a user-layer `skip` into `refuse`; main also turned an explicit `ask`). `self update` from a system gupkg in a normal shell is now refused (T09 must handle the manager's `U` key). `execute`/plan documents still carry per-package `mode` and `root_cleanup` keyed by mode (T06/T07). `selfmgmt/elevation.py` `untrusted_reason` does not recurse; `build_scope` names version dir, root, package folder, `python` and `python\Lib\site-packages` one by one; file-level ACL checks are not done (open, non-blocking). The `self update` same-root comparison uses `normpath` only (no realpath). `pkg new|check|export|bundle` and `self status` build no scope.

- Owner question, from T06 (non-blocking, decided as stated unless the owner says otherwise): a package root or `current` outside the run's root used to become an ad-hoc root and be copied in; now only a *version directory* with a `pkg.toml` outside the root is copied in (design §21.4 wording), anything else is `not inside this scope's root`. The refusal text is misleading for someone installing from a download folder. One sentence for T12 docs.
- Non-blocking, from T06 review, handed on: for T07: the catalog's kept elevated trust gate (`catalog.py` ~292-297, ~350, ~780-785, `_untrusted_reason`, `_resolve_path`) must go through `Scope.require_trusted` (the T07 grep for `is_elevated_run` will not find it, since it tests `scope.mode is SYSTEM`); merge `UntrustedLocation` (catalog.py:108) and `UntrustedLocationError` (selfmgmt/elevation.py), both mapped to `untrusted-location`; delete `health.diagnose(elevated=...)`, `Engine.elevated`; re-record `tests/fixtures/ui/list.json` and `info_alpha.json` (hand-edited in T06, still `"api": 1`) with `record.py` and make `record.py` produce a valid world (it writes a stale `[roots]` table, its Apps have no install records so they are `broken`); delete the unreachable executor "Refusing to purge" guard or test it. For T10: `gupkg_ui.plans.read_packages` returns an empty list silently when the document has no `mode` (must become a visible error). Minor test gaps: `test_catalog_one_root.py` matches only part of the "not inside this scope's root" text; no human-output test for the `(system only)` / `reason:` rows. A record with no `mode` key is accepted as not moved (schema allows it; real installs always write it); `catalog_support.activate` writes records without a mode.

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
- 2026-10-29: T03 worker reported (registry 6279294 + f9f3441); review round 1 started.
- 2026-10-29: T03 reviewed (0 blocking; CRLF in convert_035.py fixed as cc24abf); merged in the registry as da73597; registry tests 212 passed, validator OK. Check 6: 1.0.8 validator accepts the converted registry. T04 ready.
- 2026-10-29: T04 dispatched on i035/T04.
- 2026-10-29: T04 worker reported (37a6c60, 4c5db47; 3722 passed at 37a6c60); review round 1 started.
- 2026-10-29: T04 reviewed (0 blocking, 8 minor), merged as 6ab01f8; suite 3720 passed, 11 skipped. T05 dispatched.
- 2026-10-29: T05 worker reported (6497ef2, 8e2752b, 2b5b5c6, 19c12d4; 3645 passed/11 skipped); review round 1 started.
- 2026-10-29: T05 review round 1 (1 blocking: UI checksum guard; fixed in 3d9e9ab), round 2 clean; merged as fc47455; suite 3649 passed, 11 skipped. T06 dispatched.
- 2026-10-29: T06 worker reported (3b622b7, e7a3eac, e9d4ef8; 3630 passed/11 skipped); review round 1 started.
- 2026-10-29: T06 reviewed (0 blocking, several minor), merged as 955b8f0; suite 3630 passed, 11 skipped. T07 dispatched.
- 2026-10-29: T07 worker reported (7 commits, 3511 passed/11 skipped); review round 1 started.
