# Issue 031 worker log

Branch: rehaul/issue031 (from main 0d69dd0). Registry clone (a separate repository, offline, never pushed):
`<worktree>\gupkg-registry`, branch `rehaul/issue031` (git-ignored by this repository). Baseline on main: 2122 passed, 0 failed, 0 skipped.

## Scope split (owner: offline, local clone)

Done here: conversion of all 79 packages, rewrite onto the services, modes and portable declarations, package faults, the
registry tool (`src/gupkg_registry`), the fingerprint algorithm (`gupkg.services.fingerprint`), `_registry.toml`, CI workflow, the
registry's tests moved to the package checker, tests in this repository, design text, manual checks.
Manual (owner, `tests/manual_smoke.md` "Registry 2" and the registry README): real pinned versions with real hashes
(run `update`), `pin qbittorrent/5.2.3`, installing every package on a clean runner, verifying each portable mechanism
(`PORTABLE-UNVERIFIED.md`), recording the 76 HTTP fixtures and a real NSIS sample, the engine-side fingerprint use, GIMP's installer, CI.

## Files

This repository: `src/gupkg/services/fingerprint.py`; `src/gupkg_registry/{__init__,__main__,cli,layout,publish,update,validate}.py`;
`tools/snapshot_registry.py`; `tests/fixtures/registry_100/` (snapshot of the real converted registry, replaces
`tests/fixtures/registry2/`, `-text` in `.gitattributes`); 76 fixture cases in `tests/fixtures/http/` (51 new: 12 + 6 + 5 + 28; the 25 of issue 026, three of them changed);
tests `test_fingerprint`, `test_registry_tool`, `test_registry_validate`, `test_registry_100`, `test_registry_trackers`; changed
`tests/http_fixtures.py` (`GUPKG_REGISTRY_TRACKERS`), `test_track_providers`, `test_registry_expressible` (keypirinha keeps its
`portable` folder until `[nonportable]` is applied), `tools/record_http_fixtures.py`, design 12 and 17, `tests/manual_smoke.md`.
Registry clone: see its `git log` (convert, rewrite, layout and CI, LF fix).

## Decisions taken (not settled by design or issue)

- **Where the tool lives:** `src/gupkg_registry/`, a separate maintainer tool like `gupkg_migrate` (may import core, D44), not copied
  into the release bundle (the build copies only `gupkg` and `gupkg_migrate`). The registry runs it through `_tools/registry_tool.py`.
- **Fingerprint:** algorithm in the module docstring and design 17; byte-exact (no line-ending normalisation); `App` and `gupkg`
  top-level folders excluded; links refused. The registry stores files byte for byte (`* -text`) and validation requires LF in text files.
  `verify_directory` is what the engine (028) must call for a distribution folder; it can name differing files when the registry's own
  copy is given as reference.
- **`unpinned` in `_registry.toml`:** the one legacy version `qbittorrent/5.2.3` has no hash and none can be computed offline. Instead of
  deleting it (deletion is a maintainer act, D36) or committing an invented hash, validation tolerates exactly the listed versions;
  `pin` hashes one and removes the entry. Validation fails for any other version without a `sha256`.
- **No `verified` key** exists in `[portable]` (the schema rejects unknown keys and core is not mine to change), so unverified portable
  declarations are recorded by `PORTABLE-UNVERIFIED.md` (a registry test requires every declaring package to be listed) and a TOML comment.
- **Launchers** come from `gupkg_migrate.launchers` until issue 033 moves the template into core (the tool calls `write_launchers`).
  Trackers carry no launchers; every version directory gets both.
- **Registry-computed hashes** use the `--skip-checksum` machinery of 027 (`skipped-cli`, https enforced on every hop) inside the tool, then
  write `sha256_from = "registry"`; they are marked `[REVIEW]` in the output and in the `--auto-push` commit message.
- **`gupkg pkg check` (029) does not exist:** the registry's `_tools/pkg_check.py` calls `gupkg.pkgcheck.check_directory`, the only place
  that imports gupkg internals; the swap to the CLI is one function.
- Tracker fixtures now come from the snapshot (`registry_100`), removing the duplicated `registry2` copies; the registry's CI points the same tests
  at its own checkout through `GUPKG_REGISTRY_TRACKERS`.

## Findings

- Package work was done by four parallel workers on disjoint package lists; all 79 packages pass `check_directory` with 0 errors and 0 warnings, 0 hook files remain
  (limit 13). A differential test against the migration tool's own conversion of every 0.17 original shows only five intentional differences.
- Mechanical conversion kept some wrong guesses that review fixed: git `ref = "refs/heads/main"`, a `$VersionRoot\Icons\backrest.ico` that never existed,
  restic-backup-gx's asset, GIMP's `windows/` folder and development-series filter, inkscape's check URL, keypirinha's unconditional marker removal.
- Agents wrote CRLF into some registry text files; the fingerprint is byte-exact, so validation now rejects CR in text files and the registry was normalised.
- `msys2/_check_updates/config.default/configure_nsswitch.{py,cmd}` (a 0.17 post-install helper, not referenced by any manifest) is left as a support file;
  no service expresses its in-place edit of `etc/nsswitch.conf`. Owner decision whether to keep it.
- `[source.installer]` (GIMP) is parsed but no code runs it yet (issue 028).
