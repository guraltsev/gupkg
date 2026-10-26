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
