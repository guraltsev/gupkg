# Runtime modules

For operator-facing installation and recovery procedures, see
[the operations guide](../../_docs/operations.md). This file describes the
implementation-facing modules.

The Python modules in this directory are the supported implementation domains
behind the `gupkg.cli` command boundary (design section 12; the dependency rule
is `model` <- `catalog`, `services` <- `engine` <- `cli`, and nothing below
`cli` prints or reads input):

- `cli.py`: the 1.0 grammar (design 10.2), the questions it may ask, `--json`,
  `--plan`, `--events-file`, `--no-input`, the elevated relaunch and the
  forwarders `tui` and `manager`. It turns a parsed command into
  `engine.lifecycle` calls inside one `RunSession` and renders the typed result.
  `viewdata.py` builds the plain data of `list`, `info` and `--json`,
  `settingsedit.py` writes one settings file, `authoring.py` is `pkg new`, `pkg
  export` and `pkg bundle`, and `report/results.py` builds the result, plan and
  error documents (the public machine contract, `"api": 1`) and the text of
  `list`, `info` and `--plan`.
- `core.py`: `ConfigValidationError`, `read_toml_file` and the atomic writers.
- `model/`: pure data, no I/O: version ordering, `pkg.toml` format 2, settings
  schema and layering, install modes (`mode.py`), package state (`state.py`),
  the checksum policy and the plan and result types (`plan.py`).
- `catalog/`: discovery and joins over the roots and the registry caches; the
  only place package state is derived (`catalog.py`), the install record
  (`record.py`) and the registry cache (`registry.py`).
- `report/`: what a run reports. `events.py` defines the frozen event
  dataclasses (the JSON contract of the `.jsonl` logs and the events file), the
  active reporter in a `ContextVar`, `emit()` and the `phase`/`package_scope`
  helpers. `console.py` renders events with Rich on a terminal and as plain
  `==>` text otherwise. `logfile.py` writes the per-package and per-root
  `.log`/`.jsonl` files, `latest.log`, retention and `--events-file` name checks;
  `session.py` wraps one run (reporters, log files, stop marker, closing summary).
- `system/`: the one side-effect boundary. `port.py` defines the `System`
  protocol (links, environment and `PATH`, shortcuts, elevation, processes,
  foreground programs, downloads and single HTTP requests); `windows.py`
  implements it for a real machine; `fake.py` is the in-memory implementation
  behind the tests and `gupkg --sandbox DIR`. Only `system/windows.py` and
  `selfmgmt/` use `winreg`, `ctypes`, `urlopen` or start processes.
- `services/`: the built-in services that replace hook scripts (design section 8):
  the HTTP client, version-discovery providers, safe unpacking, `[[prepare]]`,
  checksum discovery and verification, fingerprints, the hook API 2 and the
  registry transports (`registrysource.py`).
- `engine/`: planning and execution of the lifecycle (design section 13).
  `planner.py` turns a package view into a plan, `executor.py` runs it,
  `appbuild.py` populates `App` atomically, `effects.py` applies, removes and
  verifies shortcuts, shims, environment values and `PATH` entries, `health.py`
  is what `check` reports and `lifecycle.py` has the entry points (`install`,
  `update`, `switch`, `uninstall`, `cleanup`, `check`, `repair`, `check_updates`,
  `link`, `run_batch`, `plan`).
- `scope.py` builds the one `Scope` of a run (its mode, from the process token, its root and
  its settings); `targets.py` resolves a target inside it; `relaunch.py` (the one elevation for a
  call, removed in issue 035 T07) and `distcopy.py` (the copy of a distribution folder into a root).
- `settingsload.py`: the settings layers a run may read.
- `selfmgmt/`: the elevated-run trust checks (`elevation.py`), where the running
  gupkg lives (`location.py`) and what `gupkg self status` reports (`status.py`).
  `bootstrap.py` loads `elevation.py` by file path before the package is
  importable, so it stays self-contained.
- `pkgcheck.py` validates a package definition (`gupkg pkg check`), `pinning.py`
  is `gupkg pkg pin`, `integrity.py` checks a version against its
  `MANIFEST.sha256`, `hooklib.py` is the library that hook modules import and
  `extractors.py` finds the bundled 7-Zip.
- `bootstrap.py`: the launcher's Python entry point (trust check for elevated
  runs, then the CLI, or a UI program when its launcher asks).

`gupkg.cmd` starts `bootstrap.py` on the bundled Python; `gupkg-tui.cmd` and
`gupkg-manager.cmd` start the two UI programs the same way (`bootstrap.py
--gupkg-program=tui|manager`), and `gupkg tui|manager ...` run them. The UI programs
(`gupkg_tui`, `gupkg_manager` and their shared library `gupkg_ui`, issue 030) sit beside the `gupkg`
package; they read through core and perform every operation as a call to the sibling `gupkg.cmd`.
These runtime modules are internal implementation details and are not a
separate public API; the public contract is the command line and its JSON documents.

The registry's CI validates its packages with `gupkg pkg check <path> --json` in
place of the library call `gupkg.pkgcheck.check_directory` that its
`_tools/pkg_check.py` made (issue 031); the registry tool itself, `gupkg_registry`,
stays outside the release bundle.
