# T10: One typed reader; the manager reads in-process; failures are visible

Issue 035: §4.5 "From issue 034"; issue 034 "Related defects" (UI parsing duplicated; silent failure); D53.

## Goal

1. **One reader.** One module turns the CLI's JSON documents into typed rows. The CLI's own `--json` output and the UI
   use the same types, so the UI never re-parses with its own rules.
2. **In-process reads.** The manager gets its inventory, info and registry status by calling the same core functions
   that `gupkg list --json`, `gupkg info --json` and `gupkg registry status --json` use. It does not spawn `gupkg.cmd`
   for reads. Every **operation** (install, update, ...) is still a CLI call.
3. **Visible failures.** Nothing the UI cannot read is dropped silently. Every failure shows on screen.

## Steps

1. Read `gupkg_ui/plans.py`, `gupkg_ui/client.py`, `gupkg/viewdata.py`, `gupkg/report/results.py`,
   `gupkg_manager/batch.py` and `gupkg_manager/app/app.py`.
2. **Create the reader:** `src/gupkg/report/documents.py`, holding frozen dataclasses for a list row, info, a plan entry
   and a result entry, and `read_list(doc)`, `read_info(doc)`, `read_plan(doc)`, `read_results(doc)`. Each:
   - checks `api == 2`;
   - raises `DocumentError` with a clear message for a missing or wrong field;
   - accepts every value the CLI really writes (for example, an `available` row has no installed version).
3. **Make `viewdata.py`/`results.py` build the documents from the same dataclasses**, so writer and reader cannot drift.
4. **Replace the parsing in `gupkg_ui/plans.py`** with the reader. Delete `plans.py`'s own field handling.
5. **Reads in-process:** `gupkg_manager/batch.py` `load_inventory` and the registry status read call the core functions
   directly with the run's scope.
6. **Visible failures:**
   - a `DocumentError`, a `ClientError` or a core exception during a read shows a one-line message in the status line;
   - the run log shows the full text;
   - `ManagerApp.read_registry` must not swallow `ClientError` silently (issue 034).
7. **Recorded documents:**
   - run the real CLI from this branch with `--sandbox` on a temporary folder;
   - record the output of `list --json`, `list --available --json`, `info --json`, `install --plan --json`,
     `install --json` (success and a failure), `registry status --json` and `check --json` into `tests/fixtures/cli_docs/`;
   - write the commands you used in `tests/fixtures/cli_docs/README.md`.
8. **Fix the test double** (`tests/ui_support.py` `FakeGupkg`): it must tell `list` and `list --available` apart (key it
   by the full argument list, not the first word), and it must serve the recorded documents.

## New tests (each must fail on `main`, then pass)

- [ ] `read_list` reads every recorded document without error, including `available` rows.
- [ ] A row with a field of the wrong type gives a `DocumentError` that names the field.
- [ ] The browser with `a` shows the available rows from the recorded `list --available` document. Paste the rendered
      screen.
- [ ] An unreadable registry status shows a message in the status line. Paste the rendered screen.

## Done when

- [ ] `gupkg_ui/plans.py` has no `dict.get(...)` field parsing left. The reader does it.
- [ ] No read in `gupkg_manager` starts a subprocess. Show it with a test that fails if the `Client` is used for `list`.
- [ ] Your log has the rendered screens (AGENTS rule 8).
- [ ] The full suite passes.
