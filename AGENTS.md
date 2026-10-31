# Agent Instructions

Consult `_docs/tests.md` for instructions on how to write tests and what kind of tests to write, and `_docs/tests_hygiene.md`
for temporary tests, retiring tests and the automatic test checks.

Consult `_docs/docstring_schema.md` for guides for writing docstrings

Consult `_docs/python_rules.md` for python style programming instructions.

## Working rules (mandatory)

These exist because guessed fixes, untested tests and unverified claims have broken real installs and wasted the owner's time. gupkg changes
machine state (elevated installs, junctions, PATH, shims), so a wrong guess is not harmless.

1. **Reproduce before fixing.** For every bug, first make it fail in front of you (a command, a script or a test) and show the failing output.
   A cause worked out only from an error message is a hypothesis. Say "hypothesis", not "root cause", until the failure is reproduced.
2. **Prove the test.** A new regression test must fail on the code without the fix and pass with it. Run both (`git stash` the source change)
   and report both results. A test that passes either way is worthless: fix it or delete it.
3. **Run the full suite** (`scripts/test.cmd`, or `pytest` from the repo root) after every source change and before saying "done". Report the
   counts. Never say "should work", "fixed" or "verified" for anything that was not run.
4. **Smallest change.** Touch only what the bug needs. No drive-by refactors, no reordering, no new abstractions in a bug fix.
5. **Verify claims about what a user runs.** The user runs the *installed* gupkg, not the source tree, and it may be older than HEAD. Before
   explaining what a screen shows or does, check the installed version and read the code of that version (`git show vX.Y.Z:path`). Do not
   describe HEAD behaviour as what the user sees.
6. **Self-update and activation are dangerous.** The running gupkg executes from `current`, and its own standard library lives behind that
   junction. Anything that removes or replaces `current`, or that runs after it, must be reproduced with a real interpreter started through
   a junction, and must never leave the package without a working `current`: build the new link first or restore the old one on every
   failure path, and test the failure path.
7. **Never leave the machine broken.** After any failed step, check `C:\opt\gupkg` / the user's roots and say exactly what state they are in
   and the one command that repairs it. Do not touch the user's installed roots without being asked.
8. **UI changes are shown, not described.** For any TUI change, render the real screen (Textual pilot / `export_text`) and paste what it
   shows, including the top hint line and the status line. Every operation must visibly report its result on screen (success, failure, what
   changed); a key that does nothing visible is a bug.
9. **One issue, one commit.** Do not commit, release or bump versions unless asked. State what is uncommitted when stopping.
10. **When unsure, stop and say so** instead of trying a second guess. Two failed attempts at the same problem: stop, report what is known,
    what is not, and what you would check next.
11. **Report honestly and briefly.** Failures first, with the output. No apologies as filler, no claims of completeness that were not checked.
12. **Walk the workflows, not just the change.** Before and after any change, list the common end-to-end workflows it touches and run each one
    for real, on the installed layout, not only the function that changed. Minimum set: install gupkg fresh; update gupkg by itself and
    with `U`; first run of the manager with an empty registry (sync, list available, install a package); update, check, repair and
    uninstall a package in user and in system mode; a failed step in each of these (offline, no admin, locked file, missing `current`) and
    the recovery from it. A workflow that ends with the user unable to launch gupkg, or unsure whether it worked, is a failure.
13. **Hunt for inconsistencies and report them.** While working, compare the things that must agree and list every mismatch you find, even
    outside the task: the key hints against the actual key bindings and the help text; the status line against the real state
    (`gupkg registry status`, install records, `current`); the docs (`_docs/`, READMEs, `--help`) against the code; the installed version
    against HEAD; the install record against what is on disk; what the CLI says with what the TUI says for the same operation. Report them
    as a list; fix only those the task covers.