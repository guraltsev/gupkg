# Operations guide

This is the runbook for installing, configuring, scheduling, repairing and
migrating `gupkg` 1.0. The top-level [README](../README.md) explains the package
model and the command reference; [Getting started](getting_started.md) and the
[cookbook](cookbook.md) are task-oriented; [Troubleshooting](troubleshooting.md)
lists every error message.

## The pieces

| Program | What it is | Start it with |
| --- | --- | --- |
| `gupkg` | The command line: every operation, no screens, no `--all`. | `gupkg ...` |
| `gupkg-tui` | One package's interactive view. A separate program that only calls `gupkg`. | `gupkg-tui NAME` or `gupkg tui NAME` |
| `gupkg-manager` | The browser over every package, and the batch commands for schedules (`check-updates --all`, `update --all`, `check`). A separate program that only calls `gupkg`. | `gupkg-manager` or `gupkg manager ...` |
| `gupkg-migrate` | The one-time converter for 0.17 data (see [Migrating](#migrating-from-017-and-018)). | `gupkg migrate ...` |

All four live in one gupkg tree, and the UI programs call the `gupkg.cmd` of **their own tree**, never one found on
`PATH`. `gupkg tui`, `gupkg manager` and `gupkg migrate` are permanent forwarders to them.

## Installing gupkg

`gupkg-bootstrap.ps1` (with `gupkg-bootstrap.cmd`, which only bypasses the script execution policy for one run) is the
supported entry point and runs on stock Windows PowerShell 5.1. It is thin: it fetches the release ZIP, verifies it,
unpacks it and runs the `install.cmd` of the version directory it holds. It writes no settings.

```bat
gupkg-bootstrap.cmd                      :: auto: system scope if elevated, otherwise user
gupkg-bootstrap.cmd -Scope user          :: force per-user, even when elevated
gupkg-bootstrap.cmd -Scope system        :: force all users, elevated shell required
gupkg-bootstrap.cmd -Source \\server\share\gupkg-1.0.0-win-amd64.zip -Sha256 <digest>
gupkg-bootstrap.cmd -SkipInstall         :: only verify and unpack
```

What it does, in order: choose the root (`%USERPROFILE%\opt` or `C:\opt`, or `-Root`); fetch the ZIP from `-Source` (a URL,
a ZIP, or in user scope an extracted folder; default: the newest row of `RELEASES.md` of the official repository) and
verify it against `-Sha256` (default: the digest in that same row; a `-Source` without `-Sha256` prints a warning);
refuse a payload without `gupkg\python\python.exe`; check every file against `gupkg\MANIFEST.sha256`; unpack to
`<root>\gupkg\<version>` (system scope: straight into an admin-only `C:\opt`, which the script creates, or refuses when an
existing `C:\opt` is not admin-only); and run that version's `install.cmd`, which starts the bundled gupkg and installs
the `gupkg` package like any other. That creates the `current` junction, the `gupkg`, `gupkg-tui` and `gupkg-manager`
shims, their `PATH` entry, the Start Menu entry, and the root data folder `<root>\.gupkg\` with the **root forwarder**
`<root>\.gupkg\gupkg.cmd` that the package launchers of that root use to find the installed gupkg. Every step is
repeatable. The only downloads are `RELEASES.md` and the ZIP, and nothing needs to be installed first.

There is one distribution: the release ZIP `gupkg-<version>-win-amd64.zip`, holding the version directory with the
embedded CPython, the hash-locked dependencies, the native shims, the programs, the package files, `gupkg\RELEASE.txt`
(version and commit) and `gupkg\MANIFEST.sha256` (one `<sha256>  <relative path>` line per file). A ZIP downloaded with a
browser and extracted with Explorer has the Mark of the Web on every file, so Windows asks before the first `install.cmd`
runs; see [the troubleshooting entry](troubleshooting.md#a-security-warning-when-running-installcmd-or-smartscreen-and-smart-app-control).

## Settings

There is one format, `settings.toml`, in layers. From lowest to highest priority:

1. built-in defaults (roots `C:\opt`, `%USERPROFILE%\opt`, `%USERPROFILE%\opt-portable`);
2. `$App\settings.toml` of the running gupkg (shipped with a release; optional);
3. the **system layer** `C:\opt\.gupkg\settings.toml` (the data folder of the system root; it may change `[roots]`);
4. the **user layer** `<user root>\.gupkg\settings.toml` (ignored in elevated and system-mode runs; a portable run uses the
   portable root's own file instead);
5. `--settings PATH` (ignored in elevated runs unless `PATH` is inside the admin-only system root).

A bundled gupkg reads the same files when they exist. Commands: `gupkg settings show` (every effective value and the layer
it came from), `settings path` (the file of each layer), `settings init` and `settings set KEY VALUE` (write the user layer;
`--mode system` writes the system layer from an administrator shell; the schema checks a value before it is written).

```toml
schema = 3
default_mode = "auto"         # auto (elevated -> system, otherwise user) | system | user

[roots]
system = 'C:\opt'
user = '%USERPROFILE%\opt'
portable = '%USERPROFILE%\opt-portable'

[integration.system]
bin = 'C:\bin'
start_menu = '%ProgramData%\Microsoft\Windows\Start Menu\opt'
[integration.user]
bin = '%USERPROFILE%\bin'
start_menu = '%APPDATA%\Microsoft\Windows\Start Menu\opt'
[integration.portable]
integrate = "user"            # user | none

[manager]                     # read only by gupkg-manager
checksum_missing = "ask"      # ask | refuse | skip
keep_screen = true            # print the last run's summary after a UI program closes

[[registry]]
name = "main"
source = "https://github.com/guraltsev/gupkg-registry.git"

[logs]
keep_runs = 50
keep_days = 90

[cleanup]
auto = false                  # true: gupkg-manager update --all ends with cleanup

[ui]
color = "auto"                # auto | always | never (NO_COLOR is honoured)

[hooks]
allow_vendor_installers = true

[install]
record_manifest = true        # false: no package records the SHA-256 of every App file
```

Machine state that is not configuration (the ad-hoc roots, the last registry sync) lives in `state.toml` beside the
settings. gupkg owns no file outside its roots: no `AppData`, no `ProgramData`, and the Windows registry is only ever
written to (an environment value, `PATH`), never read to learn anything.

## Install modes, elevation and the root forwarder

`system` installs into `C:\opt` (admin-only), `user` into `%USERPROFILE%\opt`, `portable` into the portable root with its
state in `<package>\data`. `default_mode = "auto"` means an elevated shell installs `system` and any other shell `user`; it
never means `portable`. A call that needs administrator rights relaunches gupkg **once** elevated, after one question
(`--yes` answers it, `--no-input` turns it into exit code 5), and only when this gupkg lives in an admin-only folder: a
user-writable or bundled gupkg never elevates and says that system installs need the system gupkg.

The elevated side runs only what a standard user cannot change: the bundled `python\python.exe` in isolated mode, the
version folder, the runtime and every folder on the import path must be writable by Administrators and SYSTEM only, and
the elevated launchers of a package check everything they will run, recursively, before they start. An elevated gupkg
never acts on a user or portable root (use a normal shell), and ignores the user's proxy, environment and settings layers.
A portable root is never machine-wide and never lies inside the system root.

## Checksum policy

A declared SHA-256 is always verified; a mismatch always fails. A download with none is refused by the command line unless
the call carries `--skip-checksum` (https only, this call only); `gupkg-manager` decides with `[manager] checksum_missing`:
`ask` (a question per package at a terminal; `refuse` in a schedule), `refuse`, or `skip` (relayed as
`--skip-checksum=setting`). A malformed declared hash and a plain `http://` address are never skippable, and a version with
a vendor installer needs a pinned hash. For a system target, a value from the user settings layer or from `--settings` is not
trusted. `check` and `list --details` flag installs that were not verified.

## Registries

`[[registry]]` entries name one or more registries (the official one and, for example, a private share);
`gupkg registry add NAME SOURCE` and `remove NAME` edit them. `gupkg registry sync` downloads each into
`<root>\.gupkg\registry\<name>` (cache survives self-updates; a failed sync leaves the previous validated tree). A source is
a Git URL (cloned shallow over HTTPS, no hooks, only when `git.exe` lives in an admin-only folder), the repository's archive
URL, or a ZIP on a share. Registry data is untrusted metadata: manifests are validated, never executed, and one invalid package
is reported and skipped. `registry status` and `registry list` read only the cache, `search` takes `--offline`, and
`name`, `registry/name` or `user:name` selects an entry (the first registry that has a name wins, otherwise use the prefix).
A local package follows the registry entry of the same name automatically; `gupkg link NAME [registry/name]` follows another
entry and `--unlink` follows none.

A registry package install is verified against its **fingerprint** (`_registry.toml`) when it is installed without a download;
`gupkg check` reports a mismatch.

### The registry tool

The registry (a separate repository) is kept current by `gupkg_registry`, a maintainer tool that is not part of the release
bundle (`python -m gupkg_registry`, or `_scripts\registry-tool.cmd` in the registry):

```bat
_scripts\registry-tool.cmd update [PACKAGE...]   :: run trackers, write new pinned versions with sha256 and fingerprint
_scripts\registry-tool.cmd update --auto-push    :: also commit and push (a hash the tool computed itself needs --allow-computed)
_scripts\registry-tool.cmd pin NAME/VERSION      :: give a version without a hash its first one
_scripts\registry-tool.cmd launchers             :: rewrite install.cmd, gupkg-tui.cmd and check-updates.cmd
_scripts\registry-tool.cmd validate              :: what CI runs: every tracker and version, fingerprints, launchers
```

Without `--auto-push` the tool writes the new directories and starts no `git` process; the maintainer reviews and pushes. A hash the
tool computed itself (`sha256_from = "registry"`) is flagged `[REVIEW]`. A package is its unit of failure, and `--sandbox DIR`
chooses where downloads are staged.

## Scheduled updates

Update is two commands on purpose, so a schedule can look first and switch later:

```bat
gupkg-manager check-updates --all --json   :: find and prepare new versions; activates nothing
gupkg-manager update --all --yes --json    :: activate what is prepared
gupkg-manager check --json                 :: health: drift, fingerprints
```

Run them as a **normal user**: the manager programs refuse an elevated shell, and a Task Scheduler task with "Run with highest
privileges" is refused (exit 2). From a normal shell, `check-updates` of a *system* package only reports (design 9.3, D22); an
administrator writes and prepares the new version with `gupkg check-updates NAME...` from an elevated shell, which is how a
scheduled system update runs. `update` of a system package asks for administrator rights once for the whole batch. Pass a unique
`--events-file NAME` when you want a log of the run and treat a missing `run_end` line as a failure; a call with several targets
needs `--yes` under `--no-input` (otherwise exit 5). `[cleanup] auto = true` ends an update with `gupkg cleanup` for each root
it updated.

## The health check

`gupkg check [TARGET...]` reports a package that drifted from its install record (a missing shortcut, shim or `PATH` entry), an
`App` whose files differ from the recorded fingerprint, a version that differs from its registry fingerprint, and installs that
nothing verified. `check --fix` repairs drift without downloading. `gupkg list --broken --details` and `gupkg info NAME` show the
same facts. `gupkg self status` checks gupkg's own tree against its `MANIFEST.sha256`.

## Logs

Every run that acts on a package writes `<UTC>-<command>.log` and a `.jsonl` file with the same events into
`<root>\<name>\.gupkg\logs\`; the run as a whole writes a log holding only global information and the paths of the package logs
into `<root>\.gupkg\logs\` (a run with no package, started by a bundled gupkg, logs into its own package's `.gupkg\logs\`).
`latest.log` in each folder copies the newest text log. `[logs] keep_runs` and `keep_days` (50 runs, 90 days) are applied when a
run starts, and `gupkg cleanup` prunes old logs. `gupkg logs NAME` lists them, `--path` prints one, `--open` opens it. A failure to
write a log is one warning and never fails the operation. Elevated relaunches write their events into the system root's logs; a
caller reads them there.

## Sandbox runs

`gupkg --sandbox DIR COMMAND ...` runs the real command line against a pretend machine kept in `DIR`: environment values,
`PATH`, shortcuts and elevation are recorded in `DIR` (`system.json` and `.lnk.json` records) instead of the registry and the
Start Menu, while files and downloads are real. Use it to rehearse an install, an update or an uninstall on a real machine
without touching it, and for the smoke tests in [`tests/manual_smoke.md`](../tests/manual_smoke.md). `gupkg pkg check PATH
--install` uses the same machinery with a throw-away root. A sandbox whose `system.json` says `"elevated": true` makes the CLI
ignore `--settings`.

## Migrating from 0.17 and 0.18

Core 1.0 carries no 0.17 code: no format 1 reader, no `_bootstrap` handling, no aliases. `gupkg config-check` is an unknown
command, and a format 1 `pkg.toml` is reported as an unsupported format. Migration is two one-time steps.

**1. Re-run the bootstrap (0.17 and 0.18 installs of gupkg).** 0.18's `self update` stops and names the bootstrap when it sees a
1.0 release. Run `gupkg-bootstrap.cmd` (same `-Scope` as the old install). It installs 1.0 as an ordinary package, creates the
root data folder and the root forwarder, and leaves the old version directories as they were; only an explicit `gupkg cleanup`
(and `--bundled` for bundled copies) removes them. Open a new terminal: `gupkg` is now 1.0.

**2. Convert data with the migration tool** (`gupkg migrate ...`, also callable as `gupkg-migrate`):

```bat
gupkg migrate settings C:\opt\gupkg\0.18.0\gupkg\gupkg-config.toml --check   :: print the result, change nothing
gupkg migrate settings C:\opt\gupkg\0.18.0\gupkg\gupkg-config.toml           :: write <system root>\.gupkg\settings.toml
gupkg migrate package C:\opt --check                                         :: diff every package; change nothing
gupkg migrate package C:\opt                                                 :: convert and adopt every package in the root
gupkg migrate shortcuts C:\opt\tool\1.0                                      :: .lnk files in _shortcuts -> [[shortcut]]
```

- `migrate settings` converts the 0.17 manager configuration (schema 2) into `settings.toml` (schema 3): the package roots,
  the `bin` folders and the registry source. It refuses to replace an existing settings file
  without `--force`. The 0.17 configuration lived beside `cli.py`; 1.0 reads only the layers listed under [Settings](#settings).
- `migrate package` converts a version directory, a package folder or a whole root to format 2 (`[origin]` and `[update.*]`
  become `[source]` and `[track]`; an overloaded `[origin] url` is split into `project_url`, `[track] repo` or `download_url`;
  `[[origin.versions]]` is dissolved into the single `[source]` of the matching directory, the other entries are reported and
  dropped; camelCase keys become `snake_case`; `only_portable` becomes `modes`; `ignore_checksum` is dropped with a note;
  `_bootstrap` is renamed `_check_updates`; hooks that match a known service are reported with the declarative replacement). It
  keeps a `pkg.toml.bak` (`--no-backup` opts out) and validates the result before replacing the file.
- **Adoption.** For each converted package that 0.17 installed, the tool writes `.gupkg\package.toml` from the existing
  `current` and the declared components (each marked adopted), reads the old receipts and `.gupkg\state\update.toml` once and
  removes them, and writes the 1.0 launchers `install.cmd` and `gupkg-tui.cmd` over the 0.17 `gupkg-tui.cmd`. No reinstall is
  needed, and a following `gupkg uninstall X` removes the shortcuts and wrappers that 0.17 created. `--no-adopt` only converts;
  `--mode` and `--settings` decide the mode when the root does not. A package whose active version is not converted yet is
  reported with the command that converts it.

Then `gupkg list` shows the same installed set and versions as before. The checks to run on a copy of a real 0.17 tree are
manual smoke check 032-1 in [`tests/manual_smoke.md`](../tests/manual_smoke.md).

## Self-health and recovery

```bat
gupkg self status            :: the interpreter, and every file of the running version against its MANIFEST.sha256
gupkg self repair            :: re-apply gupkg's own commands, shims and PATH entry from the installed version
gupkg self check-updates     :: the same as gupkg check-updates gupkg
gupkg self update            :: the same as gupkg update gupkg
```

`self status` is read-only; exit code 3 means the install is damaged (the installed `App` and `Icons` folders, `__pycache__` and
other install state are not release content). `self check-updates` runs gupkg's own tracker, which finds the newest row of
`RELEASES.md`; `self update` then activates the prepared version. The release ZIP is checked against the SHA-256 of its row
before anything is unpacked, a row without a digest is refused, the unpacked tree is checked against its own `MANIFEST.sha256`
and the recorded commit, nothing of the old runtime is carried over, and a failed check leaves the active version unchanged.
A release of a newer **major** version than the running gupkg is never installed this way: the tracker names the bootstrap.
Because gupkg is an ordinary package, rollback is `gupkg switch gupkg <version>`.

A missing or damaged runtime is an installation error with one fix: run `gupkg-bootstrap.cmd` again (`-Source` with a ZIP and
`-Sha256` for a vetted copy, `-Force` to replace the folder). gupkg never downloads or repairs its runtime at run time, and
never installs a dependency.

## Routine recovery

When a call fails, preserve the evidence before changing anything:

1. `gupkg check` and `gupkg registry status`; keep `gupkg list --details --json`.
2. `gupkg logs NAME --path` and read the newest log; the receipt (`<name>\.gupkg\receipts\<version>.toml`) stores the log of the
   run that wrote it.
3. For a registry problem retry `gupkg registry sync`; a failed sync keeps the previous validated cache, and `--offline` uses it.
4. For a package problem fix `pkg.toml`, run `gupkg pkg check <path>`, then `gupkg install NAME`.
5. For gupkg itself run `gupkg self status`, then re-run the bootstrap if files differ.

A failing package never invalidates the others in a batch, and a rerun revalidates what is already current.

## Release and maintenance (development tree)

Every routine job has a script in `scripts\`. They use the repository's `.venv` only and never search for another Python; without
it they exit 9009 and print how to create it. None of them ships in the release ZIP.

| Script | What it does |
|---|---|
| `test.cmd [pytest args]` | Run the test suite. |
| `run.cmd [gupkg args]` | Run `src\gupkg\gupkg.cmd` from the source tree. |
| `bump.cmd <version>` | Set the version in `_version.py` and the `pkg.toml` files and commit. |
| `release.cmd "notes" [--no-push]` | Test, build the ZIP, tag, upload it to GitHub release `v<version>`, add the `RELEASES.md` row (with the ZIP's SHA-256), and push the branch. `--no-push` keeps everything local and uploads nothing. |
| `build-standalone.cmd [output.zip]` | Build the release ZIP in `dist\` and print its SHA-256. |
| `fetch-runtime.cmd [--destination DIR] [--force]` | Fetch the pinned embeddable CPython, verify it, write `._pth` and install the locked dependencies. |

Dependency maintenance runs the tools directly: `python tools\update_deps.py` and `python tools\check_deps.py`.

`RELEASES.md` is the release registry: a table of version, full commit hash, date, SHA-256 and notes, newest first. The
SHA-256 is that of `gupkg-<version>-win-amd64.zip`, attached to GitHub release `v<version>`. gupkg's own tracker
(`pkg.local\track.py`) reads the table from `main`. To release, from a clean tree with the GitHub CLI signed in:

```bat
scripts\bump.cmd 1.0.0
scripts\release.cmd "one-line notes"
```

`tools\release.py` refuses a dirty tree, a version already listed and a ZIP that was not built from `HEAD`; it tags, pushes the tag,
creates the release with the ZIP and commits the new row after the upload, so the table never advertises an unpublished release.

Before a release run `scripts\test.cmd`. The Windows-only checks (UAC, the Start Menu, the `PATH` broadcast, a real GitHub release)
are the numbered entries of [`tests/manual_smoke.md`](../tests/manual_smoke.md).

## The registry repository

The packages live in their own repository, `gupkg-registry`, checked out beside this one (ignored here). Changing a package needs
no gupkg release: commit and push there, and the next `gupkg registry sync` picks it up. Its CI runs `gupkg pkg check <path> --json`
and the registry tool's `validate`. Top-level folders starting with `_` or `.` are repository furniture, never packages.
