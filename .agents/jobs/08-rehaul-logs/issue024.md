# Issue 024 worker log

Branch: rehaul/issue024 (from main ec75f62). Baseline: 642 passed, 0 failed, 0 skipped (main checkout's `.venv` python
run from the worktree).

## Files

New: `src/gupkg/model/state.py` (pure data: `PackageView`, `LocalState`, `InstalledState`, `RegistryEntry`, `Link`,
install record and `state.toml` parsing, status derivation, `known_candidates`), `src/gupkg/catalog/{__init__,local,record,registry,catalog}.py`.
Changed (0.17 call sites, issue scope item 7): `layout.py` (the three `resolve_input_path` / `inspect_current` /
`CurrentInspection` definitions are gone; `identity_from_path` is a thin 0.17 adapter over the catalog), `commands.py`
(`_package_record` -> `_command_package_record`, `describe_package` -> `package_summary`), `manager.py`
(`_managed_target` -> `_target_from_catalog`), `gupkg.py`, `tui.py`, `manager_tui.py`, `registry.py` (four private
transport helpers made public so the new registry module shares them), design 12 and 17 (module list, `[fingerprints]`).
Tests: `tests/catalog_support.py` (builders), `test_catalog_cases.py` (design 5.3 table), `test_catalog_state.py`,
`test_catalog_security.py`, `test_catalog_registry.py`, `test_catalog_selectors.py`, `test_install_record.py`;
changed `test_package_root_resolution.py`, `test_gupkg_cli_behaviors.py` (two tests asserted the defect).

## Readings of unclear text (decisions made)

- **`catalog/registry.py` is new; the 0.17 `gupkg/registry.py` stays** until 029 (D13: the 0.17 CLI still syncs and
  searches through it). The two share the transport (trusted Git, ZIP extraction), which is public in the old module.
  No 0.17 seed is read by the new one (D33).
- **Sync lives in `catalog/registry.py` although design 12 says catalog code never mutates**: it writes only below
  the registry's own cache folder (design 12 lists sync in this module). `describe`, `list`, `resolve` never write. The
  install record writer is in `catalog/record.py`, separate from `describe/list/resolve`, for the engine (028).
- **Fingerprint publication** is not specified by 017/031. I read it from `_registry.toml` `[fingerprints]`
  (`"<name>/<version>" = "<sha256>"`): a registry-level statement a copied version directory cannot carry itself.
  Added one line to design 17. Issue 031 may move it; the reader is one function.
- **Registry validation granularity**: a package folder with an unsafe tree (a link, a reserved name) or no valid
  version/tracker is skipped; an invalid version or tracker inside a package is dropped and reported (the rest stays).
- **Ad-hoc roots**: an `[[adhoc]]` entry names a package root (in-place install); if the path is not a package
  root itself it is scanned as a root of packages. Entries live in the `state.toml` of each configured root's data
  directory; elevated runs read only the system root's and only admin-only paths.
- **Registry cache per mode** (D20): system packages and elevated runs use the system root's cache; user and portable
  use their own root's cache, then the user's, then the system's, taking the first that has been synchronised.
- **`install` on an installed package** chooses the active version (the table's "Repair"); a not-installed package
  chooses the newest valid local version, else the newest registry version (copied in).
- **Effects verification** (drift) is opt-in (`verify_effects`) because verifying reads the Windows environment (D18);
  effect kinds known to the catalog: `shortcut`, `shim`, `file`, `env`, `path`; other kinds are listed as unverified.
  Issue 028 owns the effect schema.
- **Selector `a/b`**: a registry selector when `a` is a configured registry, otherwise a package in grouping folder `a`.
- **0.17 behaviour that stays**: the manager's per-manifest health still uses 0.17's `read_runtime_config` (format 1)
  inside `_target_from_catalog`: validating format 1 through the format 2 model would mark every 0.17 package invalid.
  Both 0.17 tests that asserted "multiple version directories" asserted the defect and now assert the fix.

## Acceptance criteria -> coverage

See the final report.
