# gupkg releases

This table is the release registry. `gupkg` updates itself by reading it from
the `main` branch: the newest row is the current release, and its commit is
downloaded as a source archive. `gupkg-bootstrap.ps1` installs the same row.

Add rows with `python tools/release.py "notes"` rather than by hand. Each row
names the commit that contains the released code, not the commit that adds the
row.

| Version | Commit | Date | Notes |
|---|---|---|---|
| 0.12.1 | `47fbfca537636d7a8a6e0215706bc35d78eeedb8` | 2026-10-16 | Add the gupkg manager shortcut |
