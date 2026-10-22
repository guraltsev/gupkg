# Issue 023: `pkg.toml` format 2, settings model, and migration tool

Date: 2026-10-22
Priority: Critical (foundation)
Epic: 020. Design: `_docs/rehaul_design.md` §4, §7, §11, §12 (`model/`),
§18 (the migration tool)

## Problem

- **`[origin]` and `[update.*]` overlap.**
  - `[origin] url` is both a ZIP origin and the repository URL for GitHub
    checks. In 36 registry packages it is a GitHub HTML page, which
    normalisation turns into a "zip" (`configuration.py:452`).
  - `[update.payload]` options (`extract`, `rename`, `extractSubdir`) work for
    zip payloads only.
- **Versions do not record their source.** A version directory created by an
  update keeps no URL or hash.
- **Key naming is mixed:** `targetPath`, `working_dir`, `Name`/`Value`,
  `ignore_checksum`, `assetName`.
- **Portable is declared twice, inconsistently.** `only_portable` and the
  `-portable` name suffix disagree in tixati and sqlitebrowser, and
  `commands.automatic_scope` uses the suffix while `gupkg.py` uses the flag.
- **Bootstrap detection is fragile.**
  - It is done by string prefix: `identity.version.startswith("bootstrap")`
    in 4 places.
  - `_bootstrap` sorts as the newest version
    (`core.compare_package_versions`), so `local_version` becomes
    `"_bootstrap"`.
- **The manager config schema 2 is narrow.** It is found only beside
  `cli.py`, has no layering, and has no place for checksum, log or UI
  settings. `state.toml` is written as JSON and parsed by hand
  (`registry.py:130`). There are four hand-written TOML emitters.

## Scope

1. **`model/version.py`.**
   - Version parsing and ordering, unchanged from 0.17 for real versions.
   - Reserved names (`current`, `_check_updates`, `data`, `.*`, `_*`) are
     excluded from ordering.
   - `is_version_dir`, `is_tracker_dir`.
2. **`model/manifest.py`.** Typed, frozen dataclasses for format 2:
   - top level: `format`, `description`, `project_url` (information only,
     never fetched), `modes`, `requires` (informational), and
     `record_manifest` (decision D41: `false` opts the package out of the
     `App` fingerprint);
   - `[source]` with `download_url` (the payload artifact), `sha256`,
     `sha256_from`, `kind`, `strip`,
     `subdir`, `nested`, `extract`, `require`, `checksum`, `installer`,
     `module`, `git.ref`/`git.commit`, and the custom populate script
     (decision D27);
   - `[track]` with `check_url` (or `repo`/`repo_url` for the
     github/gitlab/git providers), valid in `_check_updates` only
     (provider-specific fields are added and validated by issue 026);
   - `[[prepare]]`, `[portable]`, `[nonportable]`;
   - `[[shortcut]]`, `[[env]]`, `[[path]]`, `[[shim]]` (the former `[[bin]]`,
     same fields in `snake_case`).

   Parsing and validation are strict, and reject unknown keys with
   "did you mean" hints. The template language is `${version}`, with the
   filters `replace:a:b`, `major`, `minor`, `patch` and `nodots`, plus named
   captures. Templates are rejected in a pinned version, except `$App`-style
   path variables.
3. **The variables** `$Data`, `$PackageRoot` and `$Bin` are added, with
   mode-dependent validity (`$Data` is valid only when `portable` is in
   `modes`).
4. **How `App` is populated, and its kind** (design §4, decision D27).
   `[source]` names one of two populate scripts, or none:
   - **the built-in download script:** `download_url` and an optional
     `sha256`. It downloads, verifies and unpacks, then runs the package's
     remaining local install steps (`[[prepare]]`) through a hook;
   - **a custom populate script** in `pkg.local\`, named by
     `[source] script = "<path>"`: a Python module, a `.ps1`, a
     `.cmd`/`.bat` or an executable. A non-Python script runs as a
     subprocess and receives the context as JSON. A custom-populated `App`
     is as verified as the package itself, and is recoverable while the
     script's inputs exist;
   - **none:** `App` is present in the version directory and nothing
     populates it.

   `[source] local` is removed (decision D27) and is rejected as an unknown
   key.

   The kind of `App` is `[source] shipped`, a boolean:
   - **The default:** `false` when `[source]` names a populate script, and
     `true` when it does not. Tooling always writes the key explicitly.
   - **`shipped = true` takes precedence.** `shipped = true` with a
     populate script is valid. It means "included in the version directory,
     and the source is also available". gupkg treats the `App` as shipped and
     never drops or replaces it, and uses the populate script only to
     recover a missing `App`.
   - **`shipped = false` without a populate script is invalid.**
   - **A missing `App`.** An `App` that is missing or empty is recovered
     by its populate script if there is one, and for a custom populate
     script only while the script's inputs exist. Otherwise `pkg check` and
     `install` fail with the error `App is missing and no way to recover
     it.`
5. **`render_pinned(tracker, TrackResult) -> Manifest`.** It expands every
   template, removes `[track]`, and sets `sha256` and `sha256_from` only from
   a *discovered* checksum (§7.4). It is used only by `gupkg check-updates`
   (issue 028, decision D21). The rendered directory is then an ordinary
   version directory. By default `check-updates` also downloads, verifies
   and populates its `App`, but it never activates or integrates it: that
   is `update` or `install <name>@<version>` (decisions D11, D23).
6. **`model/settings.py`.** Schema 3 as in §11:
   - layered loading from built-in defaults, then `$App\settings.toml` of the
     running gupkg,
     the system root's `.gupkg\settings.toml`, the user root's
     `.gupkg\settings.toml` (skipped when elevated or in system mode). There
     is nothing in AppData or ProgramData (design §9.7, D20),
     and `--settings`;
   - each effective value reports which layer it came from (for
     `gupkg settings show`);
   - the admin-only ACL check on the system layer is reused from
     `elevation.py`.
7. **One TOML writer.** Add `tomli_w` to the runtime lock as its own
   dependency, through the dev-tree lock tooling of issue 016
   (`tools/update_deps.py`), and add it to the build probe of issue 019. Its
   only copy today is vendored inside pip, which issue 019 removes. Use it for
   settings, state, install records and receipts. Delete the hand-written
   emitters and the hand-parsed `state.toml`.
8. **No update discovery in a version directory (§9.3, decisions D10,
   D28).** A version directory is invalid if any of these hold:
   - it has a `[track]` table;
   - its `pkg.local\` contains a module that defines `track`;
   - its source, nested or checksum URLs contain templates.

   The error names `_check_updates`. `render_pinned` copies only the hook
   modules that `[source]` and `[[prepare]]` reference. Apart from this,
   version-directory scripts and hooks may use the network (decision D28),
   and validation does not inspect their imports.
9. **Format 1 files are refused.** A `pkg.toml` without `format = 2` is
   refused like any other invalid file. Core carries no 0.17 aliases, no
   format 1 hint and no `_bootstrap` handling: `_bootstrap` is just a
   reserved `_` name (decision D33). The current `configuration.py` leaves
   core and becomes the migration tool's reader (item 10).
10. **The migration tool (decision D33).** A separate, non-core program.
    It may import gupkg core and any libraries (D44); gupkg core never
    calls it and runs without it. It does four jobs:
    - **Format 1 → format 2** for a version directory, as described in
      §18. It writes a backup by default and validates the result with
      `gupkg pkg check` before replacing the file. A `--check` dry run
      prints a diff and changes nothing. It reports hooks that match a known
      service pattern, with the suggested replacement (issue 026). It
      dissolves `[[origin.versions]]`: the entry matching the directory's
      version becomes `[source]`, and the others are listed, each with a
      suggested `gupkg pkg new <name>@<version>`, and dropped.
    - **`_bootstrap` → `_check_updates`**, converted to a format 2 tracker
      in the same way.
    - **The 0.17 manager config → settings.** It converts a schema 2
      `gupkg-config.toml` into a schema 3 `<root>\.gupkg\settings.toml`:
      roots become `[roots]`, `[bin]` becomes `[integration.*].bin`, and
      `[registry]` becomes a `[[registry]]` named `main`.
    - **Adoption of 0.17 installs.** It writes an install record
      (`.gupkg\package.toml`, issue 024) for each installed 0.17 package,
      from its `current` and its declared components, and marks those
      components "adopted". It reads `.gupkg\state\update.toml` and the old
      receipts once, then removes them. Nothing is reinstalled. The install
      record comes from issue 024 and the effect records from issue 028, so
      this part is finished, and its criterion checked, once those land.

## Acceptance criteria

- [ ] **One source per version.** A `pkg.toml` with `[[origin.versions]]`,
      with any top-level `version`, `localVersion` or `name`, or with more
      than one `[source]`, is rejected. `${version}` always equals the
      version directory's name.

- [ ] **`[source] shipped`.** When the key is absent it is `true` without a
      populate script and `false` with one (a `download_url` or a custom
      populate script).
  - `shipped = false` without a populate script is rejected.
  - `shipped = true` with a `download_url` or a custom populate script is
    accepted and reported as shipped with a recovery source.
  - Validating a version whose `App` is missing or empty and that has no
    populate script fails with exactly `App is missing and no way to
    recover it.`
  - `[source] local` is rejected as an unknown key.
- [ ] **Custom populate scripts.** A version whose `[source]` names a
      Python, `.ps1`, `.cmd` or executable populate script in `pkg.local\`
      validates. A version-directory hook that imports `urllib` validates
      with no warning (decision D28).
- [ ] **The three URLs are distinct keys.** `project_url`, `[source]
      download_url` and `[track] check_url` are separate, and a `check_url`,
      `repo` or `repo_url` outside `[track]` is rejected. The 0.17 `homepage`
      and `[origin]` are rejected as unknown keys.

- [ ] Every example in §7 parses. Every 0.17 spelling (`targetPath`, `Name`,
      `ignore_checksum`, `[origin]`, `[update]`, `only_portable`, `[[bin]]`)
      is rejected as an unknown key, and a `pkg.toml` without `format = 2`
      is rejected like any other invalid file (decision D33).
- [ ] **`record_manifest`.** `record_manifest = false` validates; a
      non-boolean value is rejected (decision D41).
- [ ] `_bootstrap`, `_check_updates`, `current` and `data` never appear as
      versions in ordering, inventory or "newest version" logic. A real
      version named `bootstrap2` is an ordinary version.
- [ ] `render_pinned` on the firefox tracker example, with a fixed
      `TrackResult`, produces a manifest that has a concrete URL and no
      `[track]`. It has a `sha256` only when the result carried one.
- [ ] A version directory with `[track]`, a `pkg.local` module that defines
      `track`, or a templated source URL fails validation with an error that
      names `_check_updates`. `render_pinned` output never contains a
      tracking hook.
- [ ] The settings loader reports each effective value with its source
      layer. The user layer is ignored in elevated and system-mode runs, a
      `--settings` file outside the system root is ignored in an elevated
      run, and no layer is read from AppData or ProgramData.

Migration tool (scope item 10):

- [ ] Running the migration tool over every package of the 0.17 registry
      gives format 2 packages that `gupkg pkg check` accepts with zero
      validation errors. Each `_bootstrap` becomes `_check_updates`.
- [ ] With `--check`, the tool prints a diff and no file changes. Without
      it, the original is kept as a backup, and a result that fails
      validation leaves the original file in place.
- [ ] A `[[origin.versions]]` with three entries becomes one `[source]`
      for the directory's own version, and the run lists the other two,
      each with a suggested `gupkg pkg new <name>@<version>`.
- [ ] A schema 2 `gupkg-config.toml` converts to a settings file for which
      `gupkg settings show` reports the same roots, `bin` locations and
      registry, the registry named `main`.
- [ ] **Adoption** (checked once issues 024 and 028 have landed). On a copy
      of a real 0.17 `C:\opt` tree, after the tool runs, the first
      `gupkg list` gives the same installed set and versions, and nothing
      was reinstalled. A following `gupkg uninstall X` removes X's
      0.17-created shortcuts and wrappers. `.gupkg\state\update.toml` and
      the old receipts are gone.
- [ ] gupkg core does not need the tool: with the tool absent, format 2
      packages and settings still load and validate.

## Tests

- Parser behaviour: valid and invalid documents, with readable errors.
- Template rendering and filters.
- Settings layering and elevated filtering.
- The migration tool, run through its command line on fixture 0.17
  packages, trackers, configs and installed roots.

These test the public behaviour of `model` and of the migration tool, not
their private helpers.
