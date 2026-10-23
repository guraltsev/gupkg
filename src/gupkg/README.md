# Runtime modules and migration helpers

For operator-facing installation and recovery procedures, see
[the operations guide](../../docs/operations.md). This file describes the
implementation-facing modules and migration helpers.

The Python modules in this directory contain the supported implementation
domains used by the `gupkg.cli` command boundary and package workflows:

- `core.py`: shared result models, package identity, expansion, and atomic file
  writes; the `log_info`/`log_warning`/`log_error` adapters that turn legacy
  messages into events
- `report/`: what a run reports. `events.py` defines the frozen event
  dataclasses (the JSON contract of the `.jsonl` logs), the active reporter in a
  `ContextVar`, `emit()` and the `phase`/`package_scope` helpers; no module below
  `cli.py` prints. `console.py` renders events with Rich on a terminal and as
  plain `==>` text otherwise. `logfile.py` writes the per-package and per-root
  `.log`/`.jsonl` files, `latest.log`, retention and `--events-file` name
  checks; `session.py` wraps one run (reporters, log files, closing summary).
  Besides the event types, the log JSONL has one record the writer adds,
  `log_link` (`package`, `log`, `events`, `events_file`), in the run log.
- `system/`: the one side-effect boundary. `port.py` defines the `System`
  protocol (links, environment and `PATH`, shortcuts, elevation, processes,
  downloads); `windows.py` implements it for a real machine (junctions, registry,
  Shell COM shortcuts, `WM_SETTINGCHANGE`, child processes, HTTP); `fake.py` is
  the in-memory implementation behind the tests and `gupkg --sandbox DIR`. Only
  `system/windows.py` and `selfmgmt/` use `winreg`, `ctypes`, `urlopen` or start
  processes; every workflow receives a `System` explicitly.
- `selfmgmt/`: the elevated-run trust checks (`elevation.py`: elevation signals,
  access-control reading and the admin-only policy). `bootstrap.py` loads it by
  file path before the package is importable, so it stays self-contained.
- `layout.py`: package-path resolution and `current` activation
- `configuration.py`: canonical `pkg.toml` normalization and validation
- `metadata.py`: structure-preserving metadata synchronization and the
  starter `pkg.toml`
- `components.py`: shortcut, environment, `PATH`, and wrapper installation
- `origin.py`: Git, zip, script, and module application population
- `downloads.py`: HTTP downloads with progress and SHA-256 verification
- `updates.py`: update state, receipts, hooks, candidate validation, and staging
- `github_releases.py`: built-in latest-release discovery for GitHub assets
- `integrity.py`: the check of an installed version against its `MANIFEST.sha256`
- `collection.py`: bounded discovery of packages below a collection root
- `manager.py`: manager configuration, scoped inventory, and batch updates
- `registry.py`: ZIP-downloaded, validated registry cache, search, and selectors
- `commands.py`: the operations every front end calls (package requests, bulk
  updates, registry install, manager init, self repair)
- `outcome.py`, `configfix.py`: command results and their human/TOML
  formatting; safe `pkg.toml` creation, repair, and conversion
- `distribution.py`: standalone runtime health and scoped self-repair
- `bootstrap.py`, `extractors.py`: the launcher's Python entry point (trust
  check for elevated runs, then the CLI) and the bundled 7-Zip lookup used by
  package hooks

`gupkg/cli.py` owns only command parsing and display; all behaviour lives in `commands.py`; `gupkg/gupkg.py` exposes the
high-level install, health-check, and update workflows; `tui.py` and
`manager_tui.py` are the interactive front ends, sharing the widgets in
`tui_widgets.py`. Workflows return their errors
in an `ActionResult` instead of printing them, so each front end reports every
error exactly once.
These runtime modules are internal implementation details and are not a
separate public API. Legacy conversion remains implemented in
`legacy_to_gupkg_toml.py` and is coordinated by the public
`gupkg config-fix --output ...` command.

## Migration helpers

The scripts in this directory provide best-effort migration behavior.

They are meant to help move older package directories toward the current
`pkg.toml` schema, but they are **not** part of the supported runtime surface.
Expect to review their output manually.

## `legacy_to_gupkg_toml.py`

Builds a canonical `pkg.toml` from legacy JSON files in one package version
folder.

Typical inputs it looks for include:

- `opt_pkg.json`
- `environment*.json` or `env*.json`
- `shortcut*.json`
- `bin*.json`

The converter is intentionally best-effort:

- unknown fields are ignored
- malformed files become warnings when possible
- missing metadata may be inferred from the directory name
- legacy variable spellings are rewritten toward current package variables
- legacy `downloadURL` or `download_url` values are emitted as `[origin].url`
- existing canonical `[origin]` settings and `[[origin.versions]]` history are retained
- output is always the current top-level metadata plus optional `[origin]`,
  `[[shortcut]]`, `[[environment]]`, `[[path]]`, and `[[bin]]`

It does **not** preserve every old config shape. Review the canonical output
before installing the converted package.

Supported `gupkg` action:

```bat
gupkg.cmd config-fix --output C:\Packages\Ripgrep\v14.1.0.l1\pkg.toml C:\Packages\Ripgrep\14.1.0.local.1
```

The implementation also retains its standalone interface:

```bat
python gupkg\legacy_to_gupkg_toml.py --dir C:\Packages\Ripgrep\14.1.0.local.1 --output C:\Packages\Ripgrep\v14.1.0.l1\pkg.toml
```

From the parent `src\` directory, the convenience launcher calls the supported
action:

```bat
legacy_to_gupkg_toml.cmd C:\Packages\Ripgrep\14.1.0.local.1
```

Use `--dry-run` to print the generated TOML or `--output <path>` to choose the
destination. When the output file already exists, the converter saves it as
`pkg.toml.bak` before writing the regenerated TOML. Existing backups are
preserved with incrementing suffixes such as `pkg.toml.bak.1`.

## `shortcuts_to_gupkg_toml.py`

Imports real Windows `.lnk` files from a package version folder's `_shortcuts`
directory into that folder's existing `pkg.toml`.

The importer reads shortcut fields through Windows Script Host, converts paths
inside package-owned `App`, `Icons`, and `Shortcuts` directories back to `$App`,
`$Icons`, and `$Shortcuts`, and rewrites `[[shortcut]]` tables with matching
names. Other TOML sections are preserved, and successfully imported sources
are renamed with the `.lnk.imported` suffix.

Example:

```bat
python gupkg\shortcuts_to_gupkg_toml.py --dir C:\Packages\Ripgrep\14.1.0.local.1
```

From the parent `src\` directory, the convenience launcher calls the same
script:

```bat
shortcuts_to_gupkg_toml.cmd --dir C:\Packages\Ripgrep\14.1.0.local.1
```

Use `--dry-run` to print the updated TOML instead of writing it.

## Review checklist

After running a helper script, check at least these items:

- the package metadata matches the directory name
- shortcut targets and working directories still point where you expect
- imported shortcut names match their intended Start Menu folders
- environment variables use the intended values
- PATH entries are still appropriate
- wrapper script content still makes sense for the target shell
- `[origin].url` points at a zip archive that `gupkg` should use to populate
  `App/`
- sibling package directories should be referenced explicitly beneath
  `$VersionRoot`
- registry validation is treated as data-only; package-local modules are not
  imported while validating registry manifests

When in doubt, treat the helper output as a starting point and edit the TOML by
hand.
