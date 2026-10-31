# Temporary tests

Each folder `tests/_temp/<ref>/` holds the temporary tests of one open issue or job (`<ref>` is `issueNNN` or `jobNN`).
The rules for these folders, the README every folder needs (`ref:`, `kind:`, `purpose:`, `fate:`) and the exit review that
deletes them are in [`_docs/tests_hygiene.md`](../../_docs/tests_hygiene.md) section 3. The automatic check is rule H6 of
`tests/support/hygiene.py`.

Nothing outside `tests/_temp/<ref>/` imports from here, and a `kind: report` folder runs only when its path is given on the
command line: `scripts\test.cmd tests\_temp\<ref>\<file>.py -s`.
