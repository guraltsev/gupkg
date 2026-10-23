# gupkg releases

This table is the release registry. Each row names a version, the commit it was
built from, and the SHA-256 of the release ZIP `gupkg-<version>-win-amd64.zip`,
which is attached to the GitHub release `v<version>`. The ZIP is self-contained
(embedded CPython, locked dependencies, native shims and the application).

`gupkg manager self update` and `gupkg-bootstrap.ps1` read this table from the
`main` branch: the newest row is the current release, its ZIP is downloaded from
the release asset URL, and the download is checked against the SHA-256 in the
same row before anything is unpacked. A row marked `-` was published before ZIPs
had digests and cannot be installed by this method.

Add rows with `python tools/release.py "notes"` rather than by hand. Each row
names the commit that contains the released code, not the commit that adds the
row.

| Version | Commit | Date | SHA-256 | Notes |
|---|---|---|---|---|
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
