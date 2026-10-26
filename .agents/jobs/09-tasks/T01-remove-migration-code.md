# T01: Remove all migration code

Issue 035: §3.3, §4.10, D54.

## Goal

Delete every piece of code, test, fixture and doc text whose purpose is converting or tolerating 0.17 / 1.0.x data. This
is a mechanical deletion: it must not change how 1.0 data is handled.

## Delete these files and folders

- `src/gupkg_migrate/` (the whole folder)
- `src/gupkg/gupkg-migrate.cmd`
- `tests/test_migrate_adopt.py`, `tests/test_migrate_bundle.py`, `tests/test_migrate_package.py`,
  `tests/test_migrate_settings.py`, `tests/test_migrate_shortcuts.py`
- `tests/test_core_without_migration_tool.py`
- `tests/fixtures/legacy_017/`, `tests/fixtures/legacy_examples/`
- `tests/fixtures/registry_017/`. Delete it only **after** step 6 below.

## Edit these files

1. **`src/gupkg/bootstrap.py`:** remove the `--gupkg-program=migrate` branch (around lines 76–81) and the word `migrate`
   in its docstring.
2. **`src/gupkg/cli.py`:**
   - remove `"migrate"` from `FORWARDERS` (line 72), its help line (around line 1331) and the `program != "migrate"`
     condition (around line 1189). Keep the condition for `tui`/`manager` as it is: T09 changes it;
   - remove `_REMOVED_OPTIONS`, the class `_Removed` and every `add_argument(..., action=_Removed)` that uses them
     (around lines 1221–1249). Afterwards, those spellings are plain unknown options;
   - remove the module docstring's mention of `migrate`.
3. **`src/gupkg/engine/receipts.py`:**
   - remove `receipts_dir`, `legacy_receipt_path`, the "move the old receipt" block in `update_receipt` and the
     legacy-path branch in `read_receipt`;
   - `list_receipts` reads only `App.info`;
   - update the module docstring (remove the "1.0.0 to 1.0.x" sentence).
4. **`src/gupkg/engine/effects.py`:** remove `_LEGACY_LOOSE_FILES`, `_remove_legacy_loose_files` and its call in
   `_write_shim`.
5. **`src/gupkg/model/manifest.py`:** remove the special `nonportable` error (around lines 863–867). A `[nonportable]`
   table is then reported like any other unknown key. Check this with a test.
6. **Tests that read `registry_017` but are not migration tests:**
   - `tests/test_service_hints.py`: copy the one or two hook files it needs into a new small fixture
     `tests/fixtures/hints/`, and point the test at them. The hint scanner is a live feature of `pkg check`. Keep its
     tests;
   - `tests/test_registry_validate.py` (line ~155): same approach, or delete that one case if it only exists to read a
     0.17 hook;
   - `tests/test_catalog_registry.py`, `tests/test_registry_100.py`: delete the cases that run the migration tool or
     compare 0.17 with 1.0. Keep every case that checks 1.0 behaviour on `registry_100`.
7. **`tools/build_standalone.py`:** remove `gupkg_migrate` from the probe list (lines ~67–70), `gupkg-migrate` from the
   launcher list (~201) and from the program list (~221).
8. **Receipt tests:** delete the tests of the legacy path. Add one test showing that a version with only
   `.gupkg\receipts\<v>.toml` reads as "no receipt".
9. **Docs** (only the migration text): `README.md`, `src/gupkg/README.md`, `_docs/README.md`, `_docs/cookbook.md`,
   `_docs/operations.md`, `_docs/troubleshooting.md`, `_docs/development_guide.md`, and line 23 of
   `gupkg-bootstrap.ps1` ("0.18 moves to 1.0"). Remove the sentences. Do not rewrite anything else.

## Keep

- `src/gupkg/services/hints.py`. `gupkg pkg check` uses it.
- The `_` reserved-name rule in `catalog/local.py`. Comments that only mention `_bootstrap` may stay.

## Tests

- [ ] New: a `pkg.toml` with a `[nonportable]` table fails validation with the **generic unknown-key message**. Assert
      on that message text. Before step 5 the error exists too but has the special text, so the assertion fails there.
- [ ] New: a version with only an old receipt file has no receipt (`read_receipt(...).data is None`). It fails before
      step 3.

## Done when

- [ ] `git grep -n -i "gupkg_migrate\|gupkg-migrate\|legacy_receipt\|_LEGACY_LOOSE\|_REMOVED_OPTIONS\|registry_017\|legacy_017"`
      prints nothing.
- [ ] `git grep -n -i "migrat" -- src tools scripts` prints nothing, except `portable.py`'s "migrates the vendor's
      default configuration" (that is portable data hand-over, not migration). Write that exception in your log.
- [ ] `git grep -n "0\.17\|0\.18" -- src` prints only comments about version ordering. List each line in your log.
- [ ] The full suite passes. Note the counts.
- [ ] `.\scripts\build-standalone.cmd` is **not** run (it downloads). Instead, `python -c "import ast,sys; ast.parse(open('tools/build_standalone.py').read())"` passes.

## Do not

- Touch any mode, portable or elevation code. That is later tasks.
- Change the `[user]`/`[system]` tables or `modes`. That is T02.
