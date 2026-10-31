# gupkg releases

This table is the release registry. Each row names a version, the commit it was
built from, and the SHA-256 of the release ZIP `gupkg-<version>-win-amd64.zip`,
which is attached to the GitHub release `v<version>`. The ZIP is self-contained
(embedded CPython, locked dependencies, native shims and the application).

`gupkg self check-updates` (gupkg's own tracker; 0.17 and 0.18 call it `manager self
update`) and `gupkg-bootstrap.ps1` read this table from the `main` branch: the newest
row is the current release, its ZIP is downloaded from the release asset URL, and the
download is checked against the SHA-256 in the same row before anything is unpacked.
A row marked `-` was published before ZIPs had digests and cannot be installed by this
method.

## Changes in 1.1.1

- **A file that no package owns is no longer overwritten silently.** An install, update, switch or
  `self repair` that would write a shim, a wrapper file or a Start Menu shortcut over a file that no installed
  package owns now stops before any work, names the path, and says how to go on: move or delete the file, or
  repeat the command with `--overwrite=KINDS`. The kinds are named: `orphaned-bin-files` (a shim `.exe`, its
  `.config.toml` or a wrapper in `bin`) and `orphaned-shortcuts` (a Start Menu shortcut), for example
  `gupkg install tool --overwrite=orphaned-bin-files,orphaned-shortcuts`. A bare `--overwrite` is an error. A file
  that already holds what would be written is not a conflict. `check --fix` reports such a file and changes
  nothing. A file that appears while the install runs is refused too (the install stops before `current` moves,
  or the effect is not applied and is reported).
- **`gupkg-manager`, `gupkg-tui` and `gupkg --root DIR manager|tui` take `--root DIR`**, with the meaning it has for
  every other command. A given `--root` that does not exist or is not a folder stops the program at startup (exit
  code 2) and creates nothing; an elevated run still needs an admin-only root.
- **Known limit:** the launcher `gupkg.cmd` is a batch file and splits arguments at `=` and `,`. `--root DIR` and
  `--overwrite=...` are handled; `--skip-checksum=cli` is read as two words (`--skip-checksum` and a target `cli`),
  as it was in 1.1.0 (not fixed yet), and an empty item in `--overwrite=a,,b` is not detected through `gupkg.cmd`.

## Upgrading to 1.1 from 1.0.x

1.1.0 changes how one run is scoped. Nothing is converted automatically; read this before you update.

- **One scope per run.** An elevated gupkg manages the system root and a normal gupkg manages the user's
  root; the mode comes from the process token. `--mode` and the `system:` / `user:` selector prefixes are
  gone, and gupkg no longer starts an elevated copy of itself. To work in system mode, start gupkg or the
  manager elevated; the Start Menu entry "gupkg manager (administrator)" (installed with the system gupkg)
  does that with one click. `--root DIR` names the root of the run.
- **Settings schema 4.** A `settings.toml` with `schema = 3` is refused: change it to `schema = 4`.
  `default_mode` and the tables `[integration.system]`, `[integration.user]` and `[integration.portable]`
  are replaced by one `[integration]` table (`bin`, `start_menu`, `integrate = "full" | "none"`). `[roots]`
  keeps only `system` and `user`, and is read only from `$App\settings.toml` and `--settings`, never from a
  root's own `.gupkg\settings.toml`.
- **Portable is a kind of package.** A package is portable or not, never both: the registry now has a
  separate `<name>-portable` package for each tool that used to offer both. Install the one you want. A
  portable package installs in user mode only. `[user]` and `[system]` step tables are gone (plain
  `[[prepare]]`).
- **Registry.** Run `gupkg registry sync` after updating: this gupkg reads only the converted registry.
- **Launchers** (`install.cmd`, `gupkg-tui.cmd`, `check-updates.cmd` of a package) look for gupkg in this
  order: the copy bundled in the package, the folder named in `<root>\.gupkg\gupkg-location` (written when
  the gupkg package is installed), then `PATH`. The root forwarder is gone.
- **A package cannot take another package's names.** An install, update, switch or repair that would
  create a shim, wrapper, Start Menu shortcut or environment variable that another installed package
  already provides stops before any work, and names both packages.
- **Machine interface `"api": 2`.** `relaunched`, `child_exit_code`, `child_raw_exit_code` and the
  `child-no-result` error are gone from the JSON documents; `list`, `info` and `search` carry `mode` and
  `root` once, at document level.
- **No migration code.** `gupkg migrate` and the readers of 0.17 data are removed. A package folder
  without an install record is listed `broken`; fix it with `gupkg install <name>`. Moving from 0.17 or 0.18
  needs a 1.0.x release (below) or a fresh install.
- **Failure reports** now say what state a failed step left and the command that fixes it (`uninstall
  --purge` with a locked file, an `update` that fails after `current` moved, a package whose `current` is
  missing, `self status`).

The section below describes the move to 1.0 and applies to 1.0.x only.

## Migrating to 1.0 from 0.17 or 0.18

1.0 changes the package format (`pkg.toml` format 2), the settings file
(`settings.toml`) and the command line, and it carries no code for 0.17 data. Moving an
existing machine is two one-time steps; nothing is converted automatically.

1. **Re-run the bootstrap once.** 0.18's `self update` stops at a 1.0 release and names the
   bootstrap; 0.17 has no such notice. Download `gupkg-bootstrap.cmd` and run it with the
   scope of the old install (`-Scope system` from an elevated shell for an all-users
   install). It installs 1.0 as an ordinary package, creates the root data folder
   `<root>\.gupkg\` and the root forwarder, and leaves the old version directories
   untouched (`gupkg cleanup` removes them when you choose).
2. **Convert your data with the migration tool.** `gupkg migrate settings
   <gupkg-config.toml>` converts the manager configuration, and `gupkg migrate package
   <root>` converts every package definition (`_bootstrap` becomes `_check_updates`) and
   adopts the installed packages, so no reinstall is needed and `gupkg uninstall` still
   removes what 0.17 created. Add `--check` first to see the result without changing
   anything.

What changed for you: checking for updates and installing are two commands
(`gupkg check-updates NAME`, then `gupkg update NAME`); `gupkg manager ...` and `gupkg tui`
start the separate programs `gupkg-manager` and `gupkg-tui`; `--scope` is `--mode`;
`--no-checksum` is `--skip-checksum` (missing hashes only); `config-check` is `gupkg pkg
check`; `doctor` is `gupkg check`. The full table is in `_docs/rehaul_design.md` section 10.3
and the runbook is `_docs/operations.md`.

Add rows with `python tools/release.py "notes"` rather than by hand. Each row
names the commit that contains the released code, not the commit that adds the
row.

| Version | Commit | Date | SHA-256 | Notes |
|---|---|---|---|---|
| 1.1.5 | `5c3e29efdbdbe5a306135fed9ee7da88491301fd` | 2026-10-30 | b9f495054563dfebd89d04e96db74669244c618df2d2ec00a14e3ff7dc81cf0f | gupkg 1.1.5 |
| 1.1.3 | `9320a3ef59bec345d150019dd1a9e99c359ceb7a` | 2026-10-30 | 80d0fc39333cbb1da5b67009e0ea69b9208d08cea6b4ddde6e4937125336c567 | Add gupkg pkg adopt - package a program an installer already unpacked |
| 1.1.2 | `12523591e1f7f1162e9c386b63ef66ef09a4b764` | 2026-10-30 | dab70bad054fa9e73516b64afd0294849a646f53fdaa46300d8c6075903cf695 | gupkg 1.1.2 |
| 1.1.1 | `909b8f92488d83ef46210ac1ac33cf9990375067` | 2026-10-29 | 0d2e580e3f78b1d6a27171e25a09aa002ec80f73861013f40d080ed5c262d147 | Named --overwrite kinds for unowned files - --root for the manager and TUI |
| 1.1.0 | `908f406e0958b5eaec3b189a94241fe2562f4887` | 2026-10-29 | 96ad40619043dac3c1245f6e6c227d8ec85f7ef5f643b3bf798e6789caf8bfc8 | One scope per run - settings schema 4 - portable packages - new launcher order - api 2 |
| 1.0.8 | `3386d3eb5bdd8498406f7e0548d94c99d1894428` | 2026-10-25 | 63912039a499b9e9065128583a57b33644a4fbdbc3a2f4250209eafa619d6b4a | gupkg 1.0.8 |
| 1.0.7 | `c72a82f1dea839d0e6ab38f6dd564adb6d331aae` | 2026-10-25 | ccb410a83da145bb3184a4d6704ed31206bdb9827312aca6c247e1f7781db901 | gupkg 1.0.7 |
| 1.0.5 | `111aaff7cc6bea47ac1095567ec6d6e09053dd4a` | 2026-10-25 | 33f69fd433935bce5d96b9d91a8298db9bf3ce9bb2991a319fbbf31b5c4b0acd | gupkg 1.0.5 |
| 1.0.4 | `e0397429c0c5f775b9e3ee032d5c04c6eed73b1a` | 2026-10-25 | e6860fe8c705b136e6343a4d192552bfe2d4ac6a030e8c19ae561b25982f3ccb | gupkg 1.0.4 |
| 1.0.3 | `effcc9ac30d5292ab1019b545247e5fdd8813079` | 2026-10-25 | cff314eb0ce2b22f5c90154e74680e0ced2ea704cd20e9e519ebcd126ddebe46 | gupkg 1.0.3 |
| 1.0.2 | `296243c4c2f04970361cbdf082241af0d33e7082` | 2026-10-25 | 184817d040847cf5054ec3ef5f76c74270494167f361112c12c92c479d3c029c | gupkg 1.0.2 |
| 1.0.1 | `b505b7d59dc67a579b6678ec38f103fcf7ca6a4a` | 2026-10-24 | 442f465c6046ee4801f3dd4e5f3ec8e2448661043eefe431e8c26bcc6f84608d | gupkg 1.0.1 |
| 1.0.0 | `e8587967075f10fcc7c157c0951e4693ac84dec2` | 2026-10-24 | 22e520905afe0666051e4917be722bcc8144ae14e687b8422be65c32411a2673 | gupkg 1.0: format 2 packages, unified CLI, gupkg-tui and gupkg-manager, bundled mode, new bootstrap |
| 0.18.0 | `0a4a63ff0b287148cdedf05244f5d275f2b0c45b` | 2026-10-22 | 240d4ace40059a6ca65d85241459881f7692aa4029ff084408f16823f437d124 | Stage 0: self-contained release ZIP, native shortcuts and junctions, one bundled interpreter, no pip in the bundle |
| 0.17.0 | `800780eb9575f0455e1eaf2c71a3a543f51559e7` | 2026-10-21 | - | Template folder renamed from pkg.bootstrap to _bootstrap |
| 0.16.0 | `940ae3705bffbe8a924425706da12263ac0bed20` | 2026-10-18 | - | Folder name is the single source of identity; plain-string versions with .local.<rev>; no v prefix |
| 0.15.0 | `2081044d69fadf60e6a94c94b468022d1ae345d1` | 2026-10-17 | - | TUI progress and scrolling fixes, streamed update checks, saved update status, colors |
| 0.14.1 | `dee0b5c3167318213127ca583fbfb023dce14a8c` | 2026-10-17 | - | Point all URLs at the renamed gupkg repository |
| 0.14.0 | `e464847ea04620efe42e3ddee189fbadd96e24be` | 2026-10-17 | - | notes |
| 0.13.8 | `a33d83a593666ebb4e5c67675645af70f984580f` | 2026-10-17 | - | Fix elevated manager init (registry cache location, no leftover invalid config) |
| 0.13.7 | `be4c45399bd39f52307961da8d3cf28f3c3661f4` | 2026-10-17 | - | Fresh-clone tests, maintenance scripts, registry tag publishing, registry cache trust |
| 0.13.6 | `5f31a7e8c59dd6b64168b58765a1a39803a30f2e` | 2026-10-17 | - | Elevated runs refuse a registry cache that is not admin-only |
| 0.13.5 | `24f750ffc9823d91cb139f7ecca495f7dbb2cb08` | 2026-10-16 | - | Manager configuration is read only from the gupkg folder (no APPDATA or GUPKG_HOME); elevated runs require it admin-only |
| 0.13.4 | `e49a797a7cc92e6e9b40896a27c87ba3c47f6ea7` | 2026-10-16 | - | Fix HTTPS verification failures on github.com with the bundled Python 3.12 |
| 0.13.3 | `701473cdd21e335a32e3b3eb0f5a3b35a997970a` | 2026-10-16 | - | Install no longer tries to populate App from a Git origin |
| 0.13.2 | `d7027ba5076322d329e0cee1f1dc744bd620fe6e` | 2026-10-16 | - | Fix bundled runtime: commit .pyd modules and Lib that were gitignored |
| 0.13.1 | `abc2fa363ab0d3b420ddd7db33f8b3ddfa37fd5c` | 2026-10-16 | - | Fix elevated system-scope bootstrap: ACL hardening and bundled runtime provisioning |
| 0.13.0 | `8d0ef1abf17a0f2195aeba84cd6d906d2c154252` | 2026-10-16 | - | Trusted runtime for elevated runs: isolated bundled Python, admin-only ACL check, hash-locked dependencies with repair-deps/update-deps |
| 0.12.1 | `47fbfca537636d7a8a6e0215706bc35d78eeedb8` | 2026-10-16 | - | Add the gupkg manager shortcut |
