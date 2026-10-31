# T02: Tiers

Issue 036 F1, §4 decision 1; `_docs/tests.md` §5, §7.

## Goal

Every test module declares `unit`, `process` or `release`. The `unit` tier cannot start a process, sleep or open a
socket. `scripts\test.cmd` runs `unit` by default. `AGENTS.md` rule 3 names the tiers (after the
owner approves the wording).

## Add or change

1. **Markers** in `pytest.ini`: `unit`, `process` and `release`, each with its one-line definition from `tests.md` §5.
   Remove `windows_integration`, which becomes `process`: replace each use of it.
2. **Every existing test module gets a `pytestmark`** with its tier. Assign the tier by what the module touches, not by
   its time:
   - `process`: it starts a real child process, `cmd`, the regex worker or the real CLI, or creates a real junction,
     shortcut or `gupkg-test-*` registry key, or starts a loopback server;
   - `release`: it builds, unpacks or verifies the runtime or a release bundle (for example `test_release_bundle`,
     `test_runtime_without_pip`, `test_bootstrap_script`, `test_check_release`, `test_release_tool`,
     `test_self_release_update`, and T00's test);
   - `unit`: everything else.

   A module that mixes tiers gets per-test marks for the exceptions. Put the full list (module and tier) in your log.
3. **The hygiene plugin** (`tests/support/hygiene.py`) gains:
   - **H7:** every test has exactly one tier;
   - **H8:** while a `unit` test runs, `subprocess.Popen`, `os.system`, `socket.socket.connect` and `time.sleep` (over
     0.01 s) raise, with a message naming the tier and `tests.md` §5;
   There is no time limit per test (`tests.md` §5): time is measured by `tools\test_report.py`, not enforced.

   Existing violations of H8 go into the baseline, like those of T01 (each line removed again by the task that fixes it).
   Regenerate only the H7–H8 lines; do not touch the T01 lines.
4. **`scripts\test.cmd`:**
   - no options: `-m unit`;
   - `--process`: `-m "unit or process"`;
   - `--all`: every tier;
   - any further arguments pass through to pytest;
   - the script prints the tiers it ran on its last line.

   Update the script's comment and `_docs/development_guide.md` §Testing.
5. **`AGENTS.md` rule 3:** propose this wording in your report. Do **not** edit `AGENTS.md`; the controller asks the
   owner:

   > 3. **Run the right tier** (`_docs/tests.md` §5): `scripts\test.cmd` (`unit`) after every source change;
   > `scripts\test.cmd --process` before saying "done"; `scripts\test.cmd --all` before a merge or a commit to `main`, and
   > after any change to the build, bootstrap, self-update or activation. Report the tiers that ran and their counts.
   > Never say "should work", "fixed" or "verified" for anything that was not run.

   After the owner's answer, the controller (or a follow-up of this task) applies it.

## Done when

- [ ] `scripts\test.cmd`, `scripts\test.cmd --process` and `scripts\test.cmd --all` each pass. Paste the counts and the
      wall time of each, run serially. The `unit` time is recorded even if it is still over 30 s (later tasks bring it
      down).
- [ ] The three tiers together collect exactly the same tests as `main` (show the counts).
- [ ] A new `unit` test that calls `subprocess.run(["cmd", "/c", "exit"])` fails with the H8 message, and one that sleeps
      0.5 s fails with H8. Show both, then remove them.
- [ ] `windows_integration` appears nowhere (`git grep -n windows_integration`).
- [ ] The proposed rule 3 wording is in your report.
