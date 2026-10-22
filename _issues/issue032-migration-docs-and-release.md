# Issue 032: Migration path, documentation, issue hygiene, 1.0 release

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

1. **The migration path** (§18, D33). This issue no longer builds a
   migration command. The migration tool is a **separate, non-core
   program built in issue 023**: it converts format 1 to format 2,
   renames `_bootstrap` to `_check_updates`, converts the 0.17 manager
   config to settings, and adopts 0.17 installs by writing their install
   records. This issue documents it and runs it in the acceptance checks
   below.
   - **Core carries no legacy code.** There are no 0.17 aliases, no
     format 1 hint and no `_bootstrap` handling; `gupkg tui` and `gupkg
     manager` are permanent forwarders, not aliases (D34, issue 029).
   - **0.18 → 1.0 is a one-time bootstrap re-run.** 0.18's `self update`
     points to the bootstrap when it sees 1.0.
2. **Documentation rewrite for 1.0:**
   - **`README.md`:** the format 2 reference and the `gupkg` command
     reference. Also describe `gupkg-tui` and `gupkg-manager` (separate
     programs, D12, D34), `auto` mode (D4), and the check-updates → update
     workflow (D11, D23).
   - **`_docs/getting_started.md` and `_docs/cookbook.md`:** cover user,
     system and portable installs, the checksum policy, logs, rollback with
     `switch`, and uninstall.
   - **`_docs/operations.md`:** settings layers, multiple registries, the
     registry tool and `--auto-push` (D36), sandbox smoke tests, log
     locations (D26), the health check `gupkg check`, scheduled
     `gupkg-manager check-updates --all` / `update --all --yes`, running
     `check-updates` for system packages from an admin shell (D22), and
     the migration tool and the bootstrap re-run (D33).
   - **`_docs/troubleshooting.md`:** every new error message.
   - **`_docs/development_guide.md`:** the new module map. It becomes a
     summary of §12 and §16.
   - **`_docs/tui_style_guide.md`:** done in issue 030.
   - **`_docs/rehaul_design.md`:** marked "implemented", with the deviations
     listed.
   - **Link and file fixes:** fix the `docs/` → `_docs/` links, and move
     `docs/elevated-runtime-design.md` into `_docs/`.
3. **Manual smoke checklist.** `tests/manual_smoke.md` is rewritten around
   `--sandbox`, plus the few checks that need real Windows: UAC, the Start
   Menu and PATH broadcast.
4. **gupkg's own package in format 2** is implemented by issue 033 (bundled
   mode). This issue documents how existing 0.17 and 0.18 gupkg installs
   reach it:
   - the 1.0 bootstrap is re-run once (D33). It installs 1.0 as an
     ordinary package, which creates the root data directory and the root
     forwarder `<root>\.gupkg\gupkg.cmd`. Old version directories keep
     their layout; only an explicit `cleanup` removes them (D43);
   - the migration tool (issue 023) moves the 0.17 config from beside
     `cli.py` into `<root>\.gupkg\settings.toml`, and adopts the installed
     packages;
   - the 0.17 `gupkg-tui.cmd` files in installed packages are replaced with
     the 1.0 launchers, `install.cmd` and `gupkg-tui.cmd` (issue 033).
5. **Release 1.0.0**, using the release tooling of issue 016
   (`tools/release.py`):
   - `RELEASES.md` gets a migration section;
   - `gupkg-bootstrap.ps1` is issue 033's thin script; it neither writes
     settings nor runs `settings init`;
   - the documentation rewrite in item 2 builds on the text that issues 016
     and 018 wrote for the new distribution.
6. **Issue hygiene at the end:** move 020–031 and 033 to `_issues/_closed/` (016–019 close on their own when stage 0 ships), together
   with this issue.

## Acceptance criteria

- [ ] On a copy of a real 0.17 `C:\opt` tree and a schema 2 config, re-running
      the 1.0 bootstrap and then the migration tool (issue 023), then the
      first `gupkg list`, gives the same installed set and versions. A
      following `gupkg uninstall X` removes X's 0.17-created shortcuts and
      wrappers.
- [ ] On the same copy before migration, 1.0 `gupkg` offers no 0.17
      spelling: `gupkg config-check` is an unknown command, and a format 1
      `pkg.toml` is reported as invalid with no format 1 hint.
- [ ] No documentation link points to a missing file: a link checker runs
      over `README.md` and `_docs/`.
- [ ] `gupkg --help` and the README command reference list the same
      commands.
- [ ] `gupkg self check-updates` followed by `gupkg self update` moves an
      installed 1.0.0 to a 1.0.1 test release. The new version is verified
      against `RELEASES.md` and `MANIFEST.sha256`. gupkg's own package has
      no `download_url`: its `_check_updates` finds 1.0.1 through its custom
      `track` script, and its custom populate script fills the new `App`
      (D27).
