# gupkg releases

This table is the release registry. `gupkg` updates itself by reading it from
the `main` branch: the newest row is the current release, and its commit is
downloaded as a source archive. `gupkg-bootstrap.ps1` installs the same row.

Add rows with `python tools/release.py "notes"` rather than by hand. Each row
names the commit that contains the released code, not the commit that adds the
row.

| Version | Commit | Date | Notes |
|---|---|---|---|
| 0.13.2 | `d7027ba5076322d329e0cee1f1dc744bd620fe6e` | 2026-10-16 | Fix bundled runtime: commit .pyd modules and Lib that were gitignored |
| 0.13.1 | `abc2fa363ab0d3b420ddd7db33f8b3ddfa37fd5c` | 2026-10-16 | Fix elevated system-scope bootstrap: ACL hardening and bundled runtime provisioning |
| 0.13.0 | `8d0ef1abf17a0f2195aeba84cd6d906d2c154252` | 2026-10-16 | Trusted runtime for elevated runs: isolated bundled Python, admin-only ACL check, hash-locked dependencies with repair-deps/update-deps |
| 0.12.1 | `47fbfca537636d7a8a6e0215706bc35d78eeedb8` | 2026-10-16 | Add the gupkg manager shortcut |
