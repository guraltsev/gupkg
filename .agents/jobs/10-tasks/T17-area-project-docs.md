# T17: Area `project` (documentation), and the last flat files

Issue 036 F5. Follow the brief §4a.

## Scope

- `tests/test_documentation.py`, `test_documents.py`, `test_recorded_fixtures_current.py`, `test_module_imports.py`,
  `test_service_hints.py`;
- every test file still flat in `tests/` after T16: list them in your log and place each.

## Target layout

```text
tests/project/
  test_doc_links.py          the link and anchor checks of test_documentation
  test_command_reference.py  the README command reference equals gupkg --help
  test_recorded_documents.py recorded_fixtures_current
tests/self/test_module_imports.py    (a guard of the bundled interpreter)
```

- `test_documents` tests `report/documents`: what is left after removing the duplicates of `ui_plans` and `ui_client` goes
  to `tests/report/test_documents.py`.
- `test_service_hints` goes to `tests/services/`, next to the service whose hints it describes.

## Findings to act on (review of 2026-10-30; verify each)

- **`test_documentation`:**
  - `test_no_finished_issue_is_left_in_the_open_folder` (issues 020–033) and `test_the_readme_runs_no_removed_command`
    are repository hygiene and history, not product behaviour;
  - move the first one into the hygiene plugin, as a check of the `_issues/` folder, if the owner wants it kept (ask);
  - retire the second.
- **`test_documents`** duplicates `ui_plans` and `ui_client` (T10).
- **The new policy files** (`_docs/tests.md`, `_docs/tests_hygiene.md`) are covered by the link check like every other
  document.

## Done when

Everything in the brief §4a, plus:

- [ ] `Get-ChildItem tests\test_*.py` finds nothing. Every test lives in one of the 11 area folders of `_docs/tests.md`
      §7, and no other folder exists under `tests/` except `support/`, `fixtures/` and `_temp/`.
