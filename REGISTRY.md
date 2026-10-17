# gupkg registry revisions

This table lists published revisions of the package registry (the `pkgs`
folder). `gupkg manager registry sync` reads it from the `main` branch: the
newest row is the current revision, and its commit is downloaded as a source
archive. Registry revisions are numbered independently of gupkg releases.

Add rows with `python tools/publish_registry.py "notes"` rather than by hand.
Each row names the commit that contains the published `pkgs` tree, not the
commit that adds the row.

| Revision | Commit | Date | Notes |
|---|---|---|---|
