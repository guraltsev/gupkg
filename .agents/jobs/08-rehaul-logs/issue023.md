# Issue 023 worker log

Branch: rehaul/issue023 (from main 513cdeb).
Baseline: 355 passed, 0 failed, 0 skipped (main checkout's `.venv` python; the worktree has no `.venv`, and that
`.venv` has no `tomli_w`, so `tests/conftest.py` loads the committed runtime's copy when the import fails).

## Files

New core: `src/gupkg/model/{__init__,version,template,manifest,settings}.py`, `pkgcheck.py` (library behind
`gupkg pkg check`), `settingsload.py` (layers, elevation filtering), `tomlio.py` (the one writer).
Changed core: `core.py` (compare delegates to `model.version`), `updates.py`, `registry.py`, `components.py`,
`manager.py`, `outcome.py` (emitters moved to tomli_w), `tools/build_standalone.py` (bare probe list, migration tool
copied and probed), `tests/conftest.py`, `tests/test_runtime_without_pip.py`.
New tool: `src/gupkg_migrate/` (separate, non-core; ships in the bundle).

## Decisions and readings of unclear text

- `[source] module` (listed in the issue) is not a key: design section 4 and D27 name the custom populate script
  `script` (a Python module is `script = "pkg.local/x.py"`). A second spelling would be ambiguous.
- Templates: `${version}`, filtered references and named captures are templates. In a version directory every `${...}`
  inside `[source]` is rejected, elsewhere only `${version}`/filtered references; a plain `${USERPROFILE}` elsewhere is an
  environment reference (design 7.5 lists `${ENV}`).
- `[track]` provider-specific keys: the key sets of design 8.1 are allowed per provider, values are not validated
  (issue 026 does that).
- `configuration.py` stays in core for now: the 0.17 engine (`commands`, `gupkg`, `updates`, `registry`, `manager`,
  `collection`) still consumes it until issues 024-028 replace them. The migration tool has its own reader and does
  not import it, so deleting `configuration.py` later is a pure deletion.
- Adoption of 0.17 installs (item 10, last bullet) waits for the install record (issue 024) and effect records
  (issue 028), as the issue says; not implemented here.
- `gupkg pkg check` does not exist as a command yet (issue 029 builds the CLI); `gupkg.pkgcheck.check_directory` is the
  library it will call, and the migration tool validates through it.

## Acceptance criteria -> coverage

See the final report.
