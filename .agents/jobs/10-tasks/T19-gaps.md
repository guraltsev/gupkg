# T19: Tests for the dangerous workflows that have none

Issue 036 F12; `AGENTS.md` rules 6, 7 and 12.

## Goal

Each gap of issue 036 F12 gets one good permanent test in its feature file, proven to fail without the code it protects.
Or the owner moves it to `tests/manual_smoke.md`. This task may find real bugs. Follow `AGENTS.md` rule 1 (reproduce
first), and **do not fix product code here**: report each bug for its own issue.

## The gaps, in priority order

1. **A failed self-update activation.**
   - **What to test:**
     - a locked file;
     - offline;
     - a failure after the new version is unpacked but before `current` points to it;
     - a failure while `current` is being replaced.
   - **What must hold:** the old `current` is restored, or a working `current` exists, and the user is told the one
     command that repairs it.
   - **How:** run it with a real interpreter started through a junction (`AGENTS.md` rule 6), in the `release` tier.
     `test_junction_replace_while_running` covers only success.
2. **`check` reports a dangling `current`.** T07 may already have done this; if so, name the test.
3. **`check --fix --no-input`.**
4. **The UI activates a version**: the version picker, then a switch, then the screen shows the new version.
5. **The UI uninstalls with `--purge`.**
6. **The UI's registry sync fails**, and the failure is visible on the screen.
7. **The content of the UI's log view.**
8. **`gupkg shell` in system mode.**
9. **The cleanup's `keep_days`.**
10. **Portable `markers` on update and on switch.**
11. **The TLS handshake on a partial chain.** If this cannot be tested offline, propose a manual entry.
12. **The real `WM_SETTINGCHANGE` broadcast.** This is probably manual.

Before writing a test, check whether an earlier task of this job already added it. If so, name it.

## Done when

- [ ] Each gap has either a test (with both proof outputs in your log) or a manual entry `036-T19-N` in
      `tests/manual_smoke.md`, with the owner's agreement recorded by the controller.
- [ ] Every bug found is listed with its reproduction, and nothing in `src/` changed.
- [ ] UI tests show the rendered screen (`AGENTS.md` rule 8).
- [ ] `scripts\test.cmd --all` passes.
