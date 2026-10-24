# Issue 028 worker log

Branch: rehaul/issue028 (from main 0d69dd0). Baseline on main: 2122 passed, 0 failed, 0 skipped (235 s, repo `.venv`
Python 3.14 run from the worktree root).

## Files

New core, `src/gupkg/engine/`: `context.py` (`Engine`, trust checks, `stop_marker`), `planner.py` (pure planning),
`executor.py` (phases, receipt and install record, uninstall, cleanup), `appbuild.py` (atomic population),
`effects.py` (desired effects, apply, remove, verify), `portable.py`, `fingerprint.py`, `receipts.py`, `stateio.py`
(`[[adhoc]]`, `track.toml`), `checkupdates.py`, `health.py` (`check`), `errors.py`, `lifecycle.py` (entry points, batch,
cancel, elevation once). New pure data: `src/gupkg/model/plan.py`. New migration tool part: `src/gupkg_migrate/adopt.py`.
Changed: `model/settings.py` (`[install] record_manifest`), `model/manifest.py` (`render_pinned` expands
`${download_url}`), `model/checksum.py` (`git_gate` with a non-text commit), `system/port.py` and `system/fake.py`
(`ProcessNotAllowed`, `trusted_path_entries`), `catalog/catalog.py` (`registry_entry`), `services/gitenv.py` (trusted git),
`services/hooks.py`, `populate.py`, `prepare.py` (`forbid_track`), `pkgcheck.py` (`defines_track`), `distcopy.py` (bounds,
links, changed source), `report/logfile.py` (`package_log`), `gupkg_migrate/package.py` and `cli.py` (follow-ups, adoption),
`tools/build_standalone.py` (probe list), design 9.2, 11, 12 and a new 13.4, `src/gupkg/README.md`,
`tests/manual_smoke.md`, the issue file (notes for later issues).
New tests: `tests/engine_support.py` and `tests/test_engine_{lifecycle,cases,check_updates,check,batch,modes,sources,
distribution,effects,planner,populate,security,store}.py`, `tests/test_migrate_adopt.py`; extended
`test_distribution_copy`, `test_pkgcheck`, `test_migrate_package`, `test_checksum_fetch`, `test_checksum_policy`.

## Readings of unclear text (decisions made inside the issue's room)

- **Deletions (scope item 11, issue 023 item 9) are not done in 028, and scope item 11 stays formally unmet.** The epic
  sanctions the coexistence ("Each issue is meant to be mergeable on its own behind the existing CLI until 029 switches the
  front end", issue 020; an earlier version of this log cited D13, which is the stage-0 decision and is wrong). `cli.py`,
  `commands.py`, `tui.py`, `manager_tui.py`, `registry.py` and `manager.py` import `gupkg.py`, `updates.py`, `origin.py`,
  `components.py`, `configuration.py`, `configfix.py`, `metadata.py`, `legacy_to_gupkg_toml.py`; deleting them breaks 0.17.
  The new engine imports none of them except through the registry transports of `catalog/registry.py`. The complete
  list (also `collection.py`, `distribution.py`, `layout.py` leftovers, `pinning.py`, `outcome.py`, `commands.py`, the
  `manager.py` executor, the TUIs) is now in issue 029's text (item 12) and issue 030's item 10, with the
  prerequisite move of those transports.
- **Owners of what is not done.** The parent-junction race in `distcopy` (needs a `System` method that opens without
  following links) and the regex worker's memory cap and no-network limit (job-object limits in `WindowsSystem`) go to
  issue 032 (hardening). Locking is done in 028 (review round 1).
- **The install record's effect kinds** are `shortcut`, `shim`, `file`, `env`, `path` with the field names the catalog already
  verifies (`path`, `name`/`value`, `scope`, `entry`) plus what apply and verify need (design 13.4). Variables expand
  against `<package>\current`, so a shim or shortcut survives a switch.
- **The bin folder's PATH entry is shared** and never removed by one package; other PATH entries are removed only when the install
  added them (`added`); an environment value is deleted without being read (D18/D38: the install, update and uninstall read only `PATH`).
- **Version fingerprint** (D29): SHA-256 over file names (case folded) and content hashes; `App` only for a shipped version,
  `.gupkg` never. The registry tool (031) must compute the same. A mismatch refuses a distribution and a registry copy; for a
  version already in the root it is a report only (the user's own edit wins, 5.3).
- **Labels.** A download records its own checksum record; a version with no download copied from a distribution or a registry records
  `registry-fingerprint` or `origin-not-verified`; a script- or shipped version already in the root records no label (it makes no
  claim). `check` lists only skipped decisions and `origin-not-verified` as unverified, and shows `verified` in user and portable
  mode as a self-report.
- **Distribution folder that differs from an existing `<root>\<name>\<version>`:** the install is refused ("diverged") rather
  than silently installing the local one.
- **Cleanup with nothing installed** keeps the newest `--keep` versions; Apps newer than `current` are kept (a ready update).
  Script-populated Apps are dropped only while the recorded inputs (top-level entries of the version directory and the files of
  `pkg.local`, listed in the receipt) exist.
- **`update --download-only`** exists on the engine (prepares a known version, no activation, receipt only).
- **check-updates and D46:** a refused checksum leaves the version written without `App`, the result is `ok = false`, exit 3,
  status `new-version`, with the `checksum-missing` error (and `skippable`) and `version` set.
- **Stop marker**: `stop_marker(path)` is true once a file exists; it can only stop a run, so it is safe when elevated.
- **Elevation** is checked once per call; read-only operations (`check-updates`, `check` without `--fix`) never relaunch; an engine
  that was not told where its own gupkg is never relaunches.
- **Trust when elevated**: cache, work folder, interpreter, regex worker, 7-Zip, git (resolved from admin-only `PATH` folders only).
- **`[source.installer]`** is implemented (review round 1, owner decision): `engine/installer.py`, design 8.4 and 13.4.
- **Adoption** (`gupkg_migrate/adopt.py`): effects are the engine's own `desired_effects`, marked `adopted`; a shortcut name with a dot
  is matched to the file 0.17 wrote; the package's own PATH entries are recorded as added; old receipts (`schemaVersion`) and
  `update.toml` are read once, noted and removed; `--mode` and `--settings` decide the mode when the root does not.

## Findings while building

- `render_pinned` left `${download_url}` in `[source.checksum]`, so a tracker's sidecar template made the new version invalid (fixed).
- `portable._confined` rejected a persisted folder that was already a junction (the normal case after the first activation) (fixed).
- Replacing a junction removes the old one first: a failed switch would leave no `current`; `_activate` restores the previous one.
- `copy_distribution` did not refuse a junction at `root\<name>`; a vanishing file raised a raw `FileNotFoundError` (fixed).
- Mutation run on a copy: 54 mutants of the rules above; survivors were each turned into a test or shown equivalent (see the report).

## Review round 1, phase A (changes after the first report)

- **B2 (D19):** `--purge` unregisters an ad-hoc package only in the state of its own root, only when the package is ad hoc, before
  the folder is deleted, and an error is a warning; an elevated run never touches a user or portable `state.toml`
  (`tests/test_engine_adhoc.py`). The uninstall run was not marked ad hoc before; it is now.
- **M1:** reserved environment names (`Path`, `PATHEXT`, `TEMP`, ...) are refused by the manifest validator, `desired_effects`, and
  again by `apply_effect` and `remove_effect`. Shortcut and shim names refuse device names, streams and trailing dots.
- **M4:** per-package lock (`engine/locks.py`), `System.process_id` and `process_is_running`, `cleanup` skips a locked package and
  removes only work entries older than 24 hours; tests in `tests/test_engine_locks.py`.
- **Vendor installers:** `engine/installer.py`, the setting, the label, the guards; `tests/test_engine_installer.py` (the installer is
  always a process double).
- **Minor:** portable `hand_back` merges a replaced folder; atomic-write test by an open handle (the replace fails on Windows);
  five module-attribute patches replaced by `Engine.replace`, `Engine.clock`, a reporter hook and `FakeSystem.failures`;
  `plan_cleanup` takes `CleanupFacts.bundled`; URL credentials are redacted (`gupkg.redact`); the receipt limit is 1 MiB and a
  huge `App` records only its digest; `System.scratch_directory` replaces `tempfile` for report-only checks; `stop_marker`
  documents the unique-path rule for issue 029.
