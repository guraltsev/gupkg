# Issue 032: Migration tools, documentation, issue hygiene, 1.0 release

Date: 2026-10-22
Priority: High (closes epic 020)
Epic: 020. Design: `_docs/rehaul_design.md` §18

## Problem

The rehaul changes the package format, the settings file and the CLI.
Existing machines and package definitions need a deliberate, explicit path
forward. The documentation also has existing drift from the code:

- `README.md` links to `docs/getting_started.md` and others, but the files
  live in `_docs/`.
- `README.md` says "Downloads are checksummed" and "Versions beginning with
  `bootstrap`".
- Issue 013's configuration search order (`GUPKG_HOME`, `%APPDATA%`) is not
  implemented.
- `self update` is documented as an update, but it performs a repair.
- `docs/elevated-runtime-design.md` is the only file left in `docs/`.
- Issue 014 still uses `pkg.bootstrap` paths.

## Scope

1. **`gupkg pkg migrate <path>…`** converts 0.17 definitions as described in
   §18.
   - It writes a backup by default and validates the result before replacing
     the file.
   - `--check` performs a dry run that prints a diff.
   - It reports hooks that match known service patterns (issue 026).
   - It dissolves `[[origin.versions]]`. The entry matching the directory's
     version becomes `[source]`. The others are listed, with a suggested
     `gupkg pkg new <name>@<version>` for each, and dropped.
2. **`gupkg settings init --from <gupkg-config.toml>`** converts manager
   schema 2 to settings schema 3:
   - roots become `[roots]`;
   - `[bin]` becomes `[integration.*].bin`;
   - `[registry]` becomes `[[registry]]` named `main`.
3. **First-run adoption.** The first 1.0 run adopts installed 0.17 packages,
   writing install records as described in issue 028. It reads
   `.gupkg\state\update.toml` and the old receipts once, then removes them.
4. **Documentation rewrite for 1.0:**
   - **`README.md`:** the format 2 reference and the `gupkg` command
     reference. Also describe `gupkg-manager` (separate program, D12),
     `auto` mode (D4), and the check → update workflow (D11).
   - **`_docs/getting_started.md` and `_docs/cookbook.md`:** cover user,
     system and portable installs, the checksum policy, logs, rollback with
     `switch`, and uninstall.
   - **`_docs/operations.md`:** settings layers, multiple registries, CI
     autoupdate, sandbox smoke tests, log locations, scheduled
     `gupkg-manager check --all` / `update --all --yes`, and admitting
     system-root proposals.
   - **`_docs/troubleshooting.md`:** every new error message.
   - **`_docs/development_guide.md`:** the new module map. It becomes a
     summary of §12 and §16.
   - **`_docs/tui_style_guide.md`:** done in issue 030.
   - **`_docs/rehaul_design.md`:** marked "implemented", with the deviations
     listed.
   - **Link and file fixes:** fix the `docs/` → `_docs/` links, and move
     `docs/elevated-runtime-design.md` into `_docs/`.
5. **Manual smoke checklist.** `tests/manual_smoke.md` is rewritten around
   `--sandbox`, plus the few checks that need real Windows: UAC, the Start
   Menu and PATH broadcast.
6. **gupkg's own package in format 2** is implemented by issue 033 (bundled
   mode). This issue only migrates existing 0.17 and 0.18 gupkg installs:
   - no file move is needed. The first 1.0 self-update creates a new version
     directory whose `App` is extracted from the ZIP, and old version
     directories keep their 0.18 layout until `cleanup`;
   - that first 1.0 install creates the root data directory and root
     forwarder `<root>\.gupkg\gupkg.cmd`. Move the 0.17 config from beside
     `cli.py` into `<root>\.gupkg\settings.toml`;
   - replace the `gupkg-tui.cmd` files in installed packages with the new
     launchers.
7. **Release 1.0.0**, using the release tooling of issue 016
   (`tools/release.py`):
   - `RELEASES.md` gets a migration section;
   - `gupkg-bootstrap.ps1` is issue 033's thin script; it neither writes
     settings nor runs `settings init`;
   - the documentation rewrite in item 4 builds on the text that issues 016
     and 018 wrote for the new distribution.
8. **Issue hygiene at the end:** move 020–031 and 033 to `_issues/_closed/` (016–019 close on their own when stage 0 ships), together
   with this issue.

## Acceptance criteria

- [ ] On a copy of a real 0.17 `C:\opt` tree and a schema 2 config, running
      `settings init --from` and `pkg migrate` over every package, then the
      first `gupkg list`, gives the same installed set and versions. A
      following `gupkg uninstall X` removes X's 0.17-created shortcuts and
      wrappers.
- [ ] `pkg migrate --check` over the whole 0.17 registry reports zero
      validation errors.
- [ ] No documentation link points to a missing file: a link checker runs
      over `README.md` and `_docs/`.
- [ ] `gupkg --help` and the README command reference list the same
      commands.
- [ ] `gupkg self check` followed by `gupkg self update` moves an installed
      1.0.0 to a 1.0.1 test release. The new version is verified against
      `RELEASES.md` and `MANIFEST.sha256`, and no hook file remains in
      gupkg's own package.
