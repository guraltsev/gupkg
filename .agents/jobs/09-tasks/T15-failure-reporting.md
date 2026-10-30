# T15: A failed step says what state it left and how to recover

Added by the owner on 2026-10-29 ("Fix 4"), after T13. It answers issue 035 §9 item 9 ("Each failure reports what
happened and how to recover") and AGENTS working rules 6, 7, 10 and 11. The defects exist on `main` before job 09
(commit `d8096b5`): job 09 did not introduce them. T13's log has the reproductions (table "Findings for the owner").

## Goal

Five failure reports are wrong or silent. After this task each one tells the user, in plain words, what happened, what
state the package is in now, and the one command that fixes it. Nothing else changes: exit codes stay as they are, no
new options, no new document keys.

## The five defects (reproduce each FIRST, show the failing output, then fix)

1. **`uninstall --purge` with a locked file.** A file in the package folder is held open by another program (for
   example `App\tool.exe` running). Today the run removes the shim and `current` first, then fails with a raw
   `WinError 32` and no advice. After: the failure line says what could not be deleted and why in words
   ("`<path>` is in use by another program"), says what already happened ("the package is uninstalled: its shim and
   `current` are removed; its folder is still there"), and says what to do ("close the program, then run
   `gupkg uninstall <name> --purge` again"). A retry after the file is released must succeed (it does today; keep a
   test).
2. **`update` that fails after `current` moved.** Today the result says only `failed` while `current` already points to
   the new version and `list` shows `installed`, `check` shows `healthy`. First find out which step fails in the T13
   scenario and what state each failing step leaves. After: the failure says which step failed and what `current`
   points to now ("the package is now on 1.1.0; version 1.0.0 is kept; `gupkg check <name>` shows its state, `gupkg
   switch <name> 1.0.0` returns to the old version"). If a step that runs AFTER the switch fails and rolling back
   `current` is trivial and safe in the existing code (the code already has the old link at hand), prefer to roll
   back and say so; if not, do not add a rollback: say the truth. Decide by reading the code, and explain in your log.
   AGENTS rule 6 applies: never leave the package without a working `current`.
3. **`check` and `check --fix` with `current` missing.** A package folder that has version directories and a shim but no
   `current` entry reports `ok: not-installed`. After: it is reported as a problem of that package ("it has versions
   <list> but no active version (`current` is missing) and the shim `<name>` is still in `bin`"), with the fix named
   (`gupkg switch <name> <version>`, or `gupkg install <name>`). `check --fix` repairs it only if that is already
   what `repair` does for a missing `current`; otherwise it reports the same problem and the command, and changes
   nothing. A package folder with no versions at all and no shim stays `not-installed`.
4. **`self status` with `gupkg\current` missing.** Today it prints `ok`. After: it says that the gupkg package has no
   active version and names the repair command (`gupkg self repair`, if that works with `current` missing; check
   `selfmgmt`, otherwise the bootstrap hint).
5. **`check --fix` for a missing shipped App.** Today: "no way to recover it" with no hint. After: the same line plus
   "reinstall with: `gupkg install <name>`".

## Rules for the work

- Smallest change (AGENTS rule 4): messages and the report of state; a rollback only where item 2 allows it.
- Each fix has a test that fails on `main` and passes with the fix (AGENTS rule 2). Reproduce with real folders under
  `tmp_path` and `FakeSystem`; for the locked file use the way the existing tests make a file undeletable (look for
  `_maybe_fail`, `fail_on`, or a held-open file handle in `tests/` helpers) or hold a real open handle without
  `FILE_SHARE_DELETE` on Windows (it must be removed in a `finally`, and the test must be marked or skipped properly
  if the platform cannot do it).
- The JSON documents keep their shape: the new text goes into the existing message/reason fields. No new keys. The
  typed reader (`report/documents.py`) must still read the documents.
- Update the docs that quote the old wording (grep README.md, `_docs/troubleshooting.md`, `_docs/operations.md`) and
  the T13 manual checks `035-T13-3(b)` and `035-T13-8(c)(d)` in `tests/manual_smoke.md`: they say "Expected today ...;
  known defect ...": make them state the fixed behaviour.
- Never touch the owner's roots or the real machine.

## Done when

- [ ] For each of the five defects: the failing output on `main`, the fix, the passing output (in the log).
- [ ] The full suite passes; `git diff --check main...HEAD` is clean.
- [ ] The manual checks of T13 (3b, 8c, 8d) are updated to the fixed behaviour.
- [ ] The log says, for item 2, which step fails and what state it leaves, and whether a rollback was added (and why
      or why not).

## Do not

- Change exit codes, add options, or add keys to the JSON documents.
- Touch the registry repository.
- Fix anything else you find (list it in the report).
