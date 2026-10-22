# Issue 023: `pkg.toml` format 2 and settings model

Date: 2026-10-22
Priority: Critical (foundation)
Epic: 020. Design: `_docs/rehaul_design.md` §4, §7, §11, §12 (`model/`)

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
    in 5 places.
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
   - top level: `format`, `description`, `project_url` (was `homepage`;
     information only, never fetched), `modes`, `requires`
     (informational);
   - `[source]` with `download_url` (the payload artifact), `sha256`,
     `sha256_from`, `kind`, `strip`,
     `subdir`, `nested`, `extract`, `require`, `checksum`, `installer`,
     `module`, `git.ref`/`git.commit`;
   - `[track]` with `check_url` (or `repo`/`repo_url` for the
     github/gitlab/git providers), valid in `_check_updates` only (provider tables per issue 026);
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
4. **The kind of `App`** (design §4) is `[source] shipped`, a boolean.
   - **The default:** `false` when `[source]` has a `download_url`, `true`
     when it does not. Tooling always writes the key explicitly.
   - **`shipped = true` takes precedence.** `shipped = true` with a
     `download_url` is valid. It means "included in the version directory,
     and the source is also available". gupkg treats the `App` as shipped and
     never drops or replaces it, and uses the `download_url` only to recover
     a missing `App`.
   - **`shipped = false` without a `download_url` is invalid.**
   - **A `local` folder alone does not make `App` recoverable.**
   - **A missing `App`.** An `App` that is missing or empty is recovered
     from the `download_url` if there is one. Otherwise `pkg check` and
     `install` fail with the error `App is missing and no way to recover
     it.`
5. **`render_pinned(tracker, TrackResult) -> Manifest`.** It expands every
   template, removes `[track]`, and sets `sha256` and `sha256_from` only from
   a *discovered* checksum (§7.4). It is used only by `gupkg check` (issue 028).
   The rendered directory is then an ordinary version directory; it is never
   installed as part of the check.
6. **`model/settings.py`.** Schema 3 as in §11:
   - layered loading from built-in defaults, then the version directory,
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
8. **The network boundary (§9.3, decision D10).** A version directory is
   invalid if any of these hold:
   - it has a `[track]` table;
   - its `pkg.local\` contains a module that defines `track` or
     `check_update`;
   - its source, nested or checksum URLs contain templates.

   The error names `_check_updates`. `render_pinned` copies only the hook
   modules that `[source]` and `[[prepare]]` reference.
9. **Format 1 files are refused.** A format 1 `pkg.toml` (no `format = 2`)
   gets an error naming `gupkg pkg migrate` (issue 032). The current
   `configuration.py` stays only as the migration reader.

## Acceptance criteria

- [ ] **One source per version.** A `pkg.toml` with `[[origin.versions]]`,
      with any top-level `version`, `localVersion` or `name`, or with more
      than one `[source]`, is rejected. `${version}` always equals the
      version directory's name.

- [ ] **`[source] shipped`.** It defaults to `true` without a
      `download_url` and to `false` with one, and `pkg migrate` writes it
      explicitly.
  - `shipped = false` without a `download_url` is rejected.
  - `shipped = true` with a `download_url` is accepted, and treated as
    shipped: `cleanup` and `install --refresh` leave `App` alone.
  - A missing `App` in that case is recovered from the `download_url`.
- [ ] **The three URLs are distinct keys.** `project_url`,
      `[source] download_url` and `[track] check_url` are separate, and
      `project_url` never reaches the HTTP client: a test fails if it is
      fetched. The 0.17 `homepage` and `[origin] url` are rejected, with the
      new key named in the error.

- [ ] Every example in §7 parses. Every 0.17 spelling (`targetPath`, `Name`,
      `ignore_checksum`, `[origin]`, `[update]`, `only_portable`, `[[bin]]`)
      is rejected with a message naming the format 2 spelling.
- [ ] `_bootstrap`, `_check_updates`, `current` and `data` never appear as
      versions in ordering, inventory or "newest version" logic. A real
      version named `bootstrap2` is an ordinary version.
- [ ] `render_pinned` on the firefox tracker example, with a fixed
      `TrackResult`, produces a manifest that has a concrete URL and no
      `[track]`. It has a `sha256` only when the result carried one.
- [ ] A version directory with `[track]`, a `pkg.local/check_update.py`, or a
      templated source URL fails validation with an error that names
      `_check_updates`. `render_pinned` output never contains a tracking
      hook.
- [ ] `gupkg settings show` (or a test of the loader) lists each effective
      value with its source layer, and the user layer is ignored when the
      run is elevated.

## Tests

- Parser behaviour: valid and invalid documents, with readable errors.
- Template rendering and filters.
- Settings layering and elevated filtering.

These test the public behaviour of `model`, not its private helpers.
