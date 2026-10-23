# Issue 024 worker log

Branch: rehaul/issue024 (from main ec75f62). Baseline: 642 passed, 0 failed, 0 skipped (main checkout's `.venv` python
run from the worktree).

## Files

New: `src/gupkg/model/state.py` (pure data: `PackageView`, `LocalState`, `InstalledState`, `RegistryEntry`, `Link`,
install record and `state.toml` parsing, status derivation, `known_candidates`), `src/gupkg/catalog/{__init__,local,record,registry,catalog}.py`.
Changed (0.17 call sites, issue scope item 7): `layout.py` (the three `resolve_input_path` / `inspect_current` /
`CurrentInspection` definitions are gone; `identity_from_path` is a thin 0.17 adapter over the catalog), `commands.py`
(`_package_record` -> `_command_package_record`, `describe_package` -> `package_summary`; both take the active version from
the catalog's `PackageView` through `layout.installed_version`), `manager.py` (`_managed_target` -> `_target_from_catalog`,
which reads `current` and the versions from `resolve_package_path(...).view.local`), `gupkg.py`, `tui.py`, `manager_tui.py`, `registry.py` (four private
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

## Review round 1

- Item 2 decision: the issue text (scope item 7) says the five functions "are replaced by catalog calls" now, so I
  routed them through `PackageView` for real instead of reverting the renames: `_command_package_record` and
  `package_summary` read the installed version from the view (`layout.installed_version`), `_target_from_catalog` reads
  `current` and the version list from the view's `LocalState`; `identity_from_path` is a thin adapter that builds the
  0.17 `PackageIdentity` from `resolve_package_path`. 0.17 behaviour is kept (full suite). Manifest health in the 0.17
  manager still uses 0.17's format 1 reader.
- An elevated run with no configured roots (the bare catalog the 0.17 adapter uses) is only checked for admin-only;
  with roots, a path must be inside the system root (real path, so a junction that leaves it is refused).
- A valid `current` whose active version fails format 2 validation still shows `installed` (design 5.2); documented on
  `PackageView.status`.

### Notes for later issues

- 029 must move the shared registry transport (`trusted_git`, `is_git_source`, `archive_url`, `extract_archive`,
  `archive_revision`, `git_environment`, `git_remote_revision`, `require_trusted_cache`) and `ConfigValidationError`
  that `catalog/registry.py` imports from `gupkg.registry` and `gupkg.core` into catalog/services before deleting
  `registry.py`, and must delete `layout.identity_from_path` and `layout.installed_version` (they still accept
  `_bootstrap`, D33), together with the 0.17 `commands`/`manager` call sites.
- 031 should name `_registry.toml [fingerprints]` as the fingerprint location (design 17 now lists it).
- The catalog only stores the registry fingerprint on candidates; computing and verifying it is 027/028.

## Review round 2

- `tests/test_legacy_adapters.py` is a differential table over eight package-root shapes for the 0.17 adapters
  (`identity_from_path`, `discover_manager`, `package_summary`, `_command_package_record`). Its expectations were
  recomputed by running the same probe against the main source (ec75f62) and against this branch; the outcomes are
  identical except three intentional changes, marked `intentional` in the table:
  1. several versions with no `current` (and `_bootstrap` beside a version) resolve to the newest version instead of
     failing with "multiple version directories" (design 5.3: no `current` -> newest valid version);
  2. a `current` that leaves its package root is refused (not followed) when named, or via the package root (design 9.6);
  3. as a consequence the summary for that shape reads "No package selected" instead of showing the outside version.
- Fixed regressions: a `_bootstrap`-only root is `not-installed`/`healthy` in the manager again (the catalog does not count
  the template, so only `current` is read); a broken `current` no longer blocks an explicit version directory, only the
  package root and `current` itself.
- `\?\` paths: the prefix is stripped before the path is normalised, and the package name is built from real paths.
- Reparse-point version directories and `App` were already invalid problems (also in an elevated run); a test now says so.
- When the user root is a junction to the system root (non-elevated), dedupe lists the package once, as `system`
  (configured roots are scanned in order system, user, portable), while `resolve user:foo` still resolves it as `user`.

## Acceptance criteria -> coverage

See the final report.
