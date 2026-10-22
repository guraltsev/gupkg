# Issue 024: Catalog and package state model

Date: 2026-10-22
Priority: Critical
Epic: 020. Design: `_docs/rehaul_design.md` §3, §5, §17 (cache)

## Problem

Package state is derived ad hoc in five places, each with its own fallbacks
and errors:

- `layout.resolve_input_path`
- `layout.inspect_current`
- `manager._managed_target`
- `commands.describe_package`
- `commands._package_record`

This causes defects the owner has run into:

- **Not-installed packages with more than one version cannot be resolved.**
  A root with `_bootstrap` plus versions, or with two versions, and no
  `current` fails with "multiple version directories".
- **Registry seeds go stale.** `manager install` copies a registry seed only
  if no directory of that name exists (`commands.py:989-992`), so an old
  local `_bootstrap` silently wins.
- **The registry is never consulted after install.** Installed packages
  evolve forever from their own copied `pkg.toml`.
- **"In the registry and also local" has no meaning.** Nothing joins the two,
  and the same name can be installed in both scopes without any warning.
- **One invalid manifest makes the whole package `unhealthy`** and blocks its
  updates, even when that manifest is only `_bootstrap`.
- **Ad-hoc package paths are invisible to the manager.** A package installed
  from a path outside every root never appears in its listings.

## Scope

1. **`model/state.py`.** `PackageView`, `LocalState`, `InstalledState`,
   `RegistryEntry` and `Link`, plus the status derivation from §5.2:
   `available`, `local`, `installed`, `outdated`, `broken`, `invalid`
   (per version) and `diverged`.
2. **The install record.** `<package>\.gupkg\package.toml` is
   read and written here, holding the mode, the link (registry, name,
   revision, and the hash of the copied tracker) and the applied effects. The
   effects are written by issue 028.
3. **`catalog/local.py`.** It scans the configured roots and the ad-hoc roots
   from `state.toml`. Grouping folders work as today, through
   `gupkg-dir.toml`.
4. **`catalog/registry.py`.** The existing sync and validation, with these
   changes:
   - **Several registries:** `[[registry]]` takes a list.
   - **Cache location:** `<root>\.gupkg\registry\`, the root data
     directory (design §9.7). System runs use only the system root's cache.
     It is no longer inside the gupkg
     version's `App`.
   - **Per-package validation:** an invalid entry is reported and skipped.
     It no longer fails the whole tree.
   - **Validated once per revision:** a validated index is cached, so
     `search` stops re-validating everything.
   - **Entries in both forms:** registry 2 entries (`<version>/` and
     `_check_updates/`) and, for the transition, 0.17 seeds.
5. **`catalog/catalog.py`.**
   - `describe(selector) -> PackageView`.
   - `list(filters)`.
   - `resolve(selector, for_mutation)`, which implements the selector grammar
     of §10.1. A name found in several roots is ambiguous for mutating
     commands.
   - `candidates(view) -> list[Candidate]`: the newer local versions,
     including those written by earlier checks, and the linked registry's
     pinned versions. It is pure, and **never includes running a tracker**
     (D11). Install and update choose only from these.
   - `compare(view, TrackResult)`: used by `gupkg check` to decide whether
     a tracker result is newer than every known version.
6. **The behaviour of the §5.3 table**, in particular:
   - a not-installed package with any number of versions installs the newest
     valid one;
   - copying a version in never overwrites, and a differing local copy is
     reported as `diverged`;
   - the registry tracker is copied in and refreshed unless it was edited
     locally;
   - packages with the same name but no link are shown as
     "same name in registry" and are never merged;
   - a same-name package in both system and user roots produces a "shadowing"
     warning in `list` and `doctor`.
7. **The old call sites are deleted.** The five functions above are replaced
   by catalog calls.

## Acceptance criteria

- [ ] A table-driven test covers every row of §5.3 and asserts on `list`
      status and on the chosen install or update candidate. It builds roots
      on a temporary directory with a fixture registry cache. Each row is its
      own test case, labelled by an `id`.
- [ ] A root containing `_check_updates` plus two versions, with no
      `current`, installs the newest valid version, and `list` shows it as
      `local` with its real version, not `_bootstrap`.
- [ ] An invalid `_check_updates` marks only the tracker as invalid. The
      package's installed version still updates from local and registry
      versions.
- [ ] Installing `firefox` from the registry when `user\firefox\_bootstrap`
      already exists does not silently reuse the stale seed. A migrated
      tracker is refreshed, or reported as diverged.
- [ ] A package installed from an ad-hoc path appears in `gupkg list`, and
      in `gupkg-manager update --all`.
- [ ] For a package that has only `_check_updates`, `install` reports
      "no version known yet; run `gupkg check <name>`" and makes no network
      request.

## Out of scope

Executing installs (issue 028). This issue produces views, selections and
candidate lists only.
