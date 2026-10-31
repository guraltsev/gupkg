# Operations guide

This is the runbook for installing, configuring, scheduling, repairing and
updating `gupkg` 1.0. The top-level [README](../README.md) explains the package
model and the command reference; [Getting started](getting_started.md) and the
[cookbook](cookbook.md) are task-oriented; [Troubleshooting](troubleshooting.md)
lists every error message.

## The pieces

| Program | What it is | Start it with |
| --- | --- | --- |
| `gupkg` | The command line: every operation, no screens, no `--all`. | `gupkg ...` |
| `gupkg-tui` | One package's interactive view. A separate program that only calls `gupkg`. | `gupkg-tui NAME` or `gupkg tui NAME` (`--root DIR` names the root) |
| `gupkg-manager` | The browser over every package, and the batch commands for schedules (`check-updates --all`, `update --all`, `check`). A separate program that only calls `gupkg`. | `gupkg-manager` or `gupkg manager ...` (`gupkg-manager --root DIR` opens it on another root) |

All four live in one gupkg tree, and the UI programs call the `gupkg.cmd` of **their own tree**, never one found on
`PATH`. `gupkg tui` and `gupkg manager` are pass-through commands that start them. Each program runs in one scope, the mode of its own process (an administrator shell manages the system root, any other shell the user's root).

## Installing gupkg

`gupkg-bootstrap.ps1` (with `gupkg-bootstrap.cmd`, which only bypasses the script execution policy for one run) is the
supported entry point and runs on stock Windows PowerShell 5.1. It is thin: it fetches the release ZIP, verifies it,
unpacks it and runs the `install.cmd` of the version directory it holds. It writes no settings.

```bat
gupkg-bootstrap.cmd                      :: auto: system scope if elevated, otherwise user
gupkg-bootstrap.cmd -Scope user          :: per-user (refused from an elevated shell)
gupkg-bootstrap.cmd -Scope system        :: force all users, elevated shell required
gupkg-bootstrap.cmd -Source \\server\share\gupkg-1.0.0-win-amd64.zip -Sha256 <digest>
gupkg-bootstrap.cmd -SkipInstall         :: only verify and unpack
```

What it does, in order: choose the root (`%USERPROFILE%\opt` in user scope, `C:\opt` or `-Root` in system scope); fetch the ZIP from `-Source` (a URL,
a ZIP, or in user scope an extracted folder; default: the newest row of `RELEASES.md` of the official repository) and
verify it against `-Sha256` (default: the digest in that same row; a `-Source` without `-Sha256` prints a warning);
refuse a payload without `gupkg\python\python.exe`; check every file against `gupkg\MANIFEST.sha256`; unpack to
`<root>\gupkg\<version>` (system scope: straight into an admin-only `C:\opt`, which the script creates, or refuses when an
existing `C:\opt` is not admin-only); and run that version's `install.cmd`, which starts the bundled gupkg and installs
the `gupkg` package like any other. That creates the `current` junction, the `gupkg`, `gupkg-tui` and `gupkg-manager`
shims, their `PATH` entry, the Start Menu entry (in system mode also "gupkg manager (administrator)", whose link asks Windows for administrator rights), and the root data folder `<root>\.gupkg\` with the one-line file
`<root>\.gupkg\gupkg-location` (the folder that holds `gupkg.cmd`) that the package launchers of that root use to find the installed gupkg. Every step is
repeatable. The only downloads are `RELEASES.md` and the ZIP, and nothing needs to be installed first.

There is one distribution: the release ZIP `gupkg-<version>-win-amd64.zip`, holding the version directory with the
embedded CPython, the hash-locked dependencies, the native shims, the programs, the package files, `gupkg\RELEASE.txt`
(version and commit) and `gupkg\MANIFEST.sha256` (one `<sha256>  <relative path>` line per file). A ZIP downloaded with a
browser and extracted with Explorer has the Mark of the Web on every file, so Windows asks before the first `install.cmd`
runs; see [the troubleshooting entry](troubleshooting.md#a-security-warning-when-running-installcmd-or-smartscreen-and-smart-app-control).

## Settings

There is one format, `settings.toml` (`schema = 4`), in layers. A run has one scope (one mode, one root) and reads only its own
layers, lowest priority first:

1. built-in defaults of the mode (system: `C:\opt`, `C:\bin`; user: `%USERPROFILE%\opt`, `%USERPROFILE%\bin`);
2. `$App\settings.toml` of the running gupkg (shipped with a release; optional);
3. `<root>\.gupkg\settings.toml`, the data folder of the run's root;
4. `--settings PATH`, **user mode only** (a system-mode run refuses the option).

There is no system layer for a user run and no user layer for a system run, and no machine-wide policy: each mode has its own
settings, registry cache, state and logs and never reads the other's. `[roots]` (keys `system` and `user`) is read only from
layers 2 and 4, never from a root's own file, because a root cannot name itself. A bundled gupkg reads the same files when they
exist. The root of the run comes from `--root DIR` first (in system mode it must be admin-only), then from a fixed chain per mode
(system: the root of the installed system gupkg, `[roots].system` of `$App\settings.toml`, `C:\opt`; user: `[roots].user` of
`--settings`, the root this gupkg is installed in when it is not admin-only, `[roots].user` of `$App\settings.toml`,
`%USERPROFILE%\opt`); the Resolve phase prints the root and what named it. Commands: `gupkg settings show` (every effective value and the layer
it came from), `settings path` (the file of each layer), `settings init` and `settings set KEY VALUE` (write layer 3, the run's
root; the schema checks a value before it is written). A file with `schema = 3` is refused with a message that says what to change.

```toml
schema = 4

[roots]                       # read only from $App\settings.toml and --settings
system = 'C:\opt'
user = '%USERPROFILE%\opt'

[integration]
bin = '%USERPROFILE%\bin'     # default C:\bin in system mode
start_menu = '%APPDATA%\Microsoft\Windows\Start Menu\opt'   # default is the %ProgramData% one in system mode
integrate = "full"            # full | none (none: shims in <root>\bin, no Start Menu, no environment)
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

[adopt]
shortcut_prefix = ""          # `gupkg pkg adopt`: a folder put before each shortcut name, relative to integration.start_menu; "" for none
```

`shortcut_prefix` is relative to `integration.start_menu`, never a full path, and may not contain `..`; `pkg adopt --prefix`
overrides it for one call. Where a root's shortcuts go is `integration.start_menu`. Put it in the root's own
`<root>\.gupkg\settings.toml` to give that root its own Start Menu folder, for example for a games root:

```bat
gupkg --root C:\Games settings set integration.start_menu "%APPDATA%\Microsoft\Windows\Start Menu\Games"
```

Machine state that is not configuration (the last registry sync) lives in `state.toml` beside the
settings. gupkg owns no file outside its roots: no `AppData`, no `ProgramData`, and the Windows registry is only ever
written to (an environment value, `PATH`), never read to learn anything.

## Scopes, modes and elevation

A run belongs to one scope: a mode and one root. The mode is that of the process token and nothing else. An elevated process is
in `system` mode and works on `C:\opt` (admin-only), any other process is in `user` mode and works on `%USERPROFILE%\opt`. There
is no mode option, no setting that selects a mode, no mode prefix on a target and no "auto". gupkg contains no code that
starts an elevated copy of itself: to manage system packages, start gupkg or `gupkg-manager` from an administrator shell, or from
the Start Menu entry "gupkg manager (administrator)" that installing the gupkg package in system mode creates (its link carries the
"run as administrator" flag, so Windows shows the UAC prompt). On a machine where every process is elevated (UAC off, a CI runner)
there is only system mode.

A package says which modes it may be installed in (`modes`). It is checked in the planner, before any download or file change,
for `install`, `update`, `switch`, repair and `check-updates` with download; `uninstall`, `check` and `cleanup` are never refused
by it. The refusal says "This package installs only for a single user. Run gupkg without administrator rights." or "This package installs only system-wide. Run gupkg as administrator: use the Start Menu entry "gupkg manager (administrator)", or run in an administrator shell: gupkg <command> <name>", and names nothing of the other mode. An `update` of a package that is already installed in the user root, to a version that allows only the system, says "<name> <version> installs only system-wide, so it cannot be installed in this user root. The installed version <installed> keeps working." and gives no administrator advice. A **portable** package (`modes = ["portable"]`, with a `[portable]` table, state
in `<package>\data`) installs in user mode only, and is a package of its own (`<name>-portable`); nothing in the system root keeps
state a standard user can write.

The elevated side runs only what a standard user cannot change: the bundled `python\python.exe` in isolated mode, the version
folder, the runtime and every folder on the import path must be writable by Administrators and SYSTEM only, and the elevated
launchers of a package check everything they will run, recursively, before they start (see "Package launchers"
in the [README](../README.md#on-disk-layout)). An elevated gupkg started from any other tree refuses (exit 2). A user-mode run
never reads anything under the system root except the gupkg program it runs: "code may be shared, data never".

A root kept elsewhere is its own scope: `gupkg --root D:\tools\opt ...` (a user root; a system root must be admin-only). A root on a
stick with `integrate = "none"` in its own `.gupkg\settings.toml` touches neither the registry nor the Start Menu, and is how a
portable setup is carried. The user's manager does not list another root's packages.
## Checksum policy

A declared SHA-256 is always verified; a mismatch always fails. A download with none is refused by the command line unless
the call carries `--skip-checksum` (https only, this call only); `gupkg-manager` decides with `[manager] checksum_missing`:
`ask` (a question per package at a terminal; `refuse` in a schedule), `refuse`, or `skip` (relayed as
`--skip-checksum=setting`). A malformed declared hash and a plain `http://` address are never skippable, and a version with
a vendor installer needs a pinned hash. In a system-mode run `--settings` is refused, so only the system root's own settings count. `check` and `list --details` flag installs that were not verified.

## Registries

`[[registry]]` entries name one or more registries (the official one and, for example, a private share);
`gupkg registry add NAME SOURCE` and `remove NAME` edit them. `gupkg registry sync` downloads each into
`<root>\.gupkg\registry\<name>` (cache survives self-updates; a failed sync leaves the previous validated tree). A source is
a Git URL (cloned shallow over HTTPS, no hooks, only when `git.exe` lives in an admin-only folder), the repository's archive
URL, or a ZIP on a share. Registry data is untrusted metadata: manifests are validated, never executed, and one invalid package
is reported and skipped. `registry status` and `registry list` read only the cache, `search` takes `--offline`, and
`name` or `registry/name` selects an entry (the first registry that has a name wins, otherwise use the prefix `registry/`).
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
chooses where downloads are staged (a folder that is kept also keeps its download cache, so a rerun does not download again).

`update` prints `[n/total] name ... <outcome>` for each package as it finishes, so a long run shows what it is doing, and ends with a
summary: the counts (`62 added, 17 failed of 79 packages`), then the packages updated with their versions, the ones whose hash the
tool computed (to review before pushing), the ones whose download address has no version in it, the failed ones with the reason,
and the ones that were already current.

Only one run may use a registry checkout at a time: `update` and `pin` hold a lock file (`.registry-tool.lock`) that names the
process, host, operation and start time. A second run stops with that message before it writes anything. A lock whose process has
ended (a crash or a closed window), or one older than a day, is taken over by the next run, so nothing has to be deleted by hand.

## Scheduled updates

Update is two commands on purpose, so a schedule can look first and switch later:

```bat
gupkg-manager check-updates --all --json   :: find and prepare new versions; activates nothing
gupkg-manager update --all --yes --json    :: activate what is prepared
gupkg-manager check --json                 :: health: drift, fingerprints
```

Run them as the account whose packages they manage. From a normal shell they act on the user's root. A Task Scheduler task with "Run
with highest privileges" acts on the system root, which needs the gupkg installed in `C:\opt`; the manager never asks for
administrator rights and a user-mode run never reaches the system root. Pass a unique
`--events-file NAME` when you want a log of the run and treat a missing `run_end` line as a failure; a call with several targets
needs `--yes` under `--no-input` (otherwise exit 5). `[cleanup] auto = true` ends an update with `gupkg cleanup` for the root it
updated.
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
write a log is one warning and never fails the operation.

## Sandbox runs

`gupkg --sandbox DIR COMMAND ...` runs the real command line against a pretend machine kept in `DIR`: environment values,
`PATH`, shortcuts and elevation are recorded in `DIR` (`system.json` and `.lnk.json` records) instead of the registry and the
Start Menu, while files and downloads are real. Use it to rehearse an install, an update or an uninstall on a real machine
without touching it, and for the smoke tests in [`tests/manual_smoke.md`](../tests/manual_smoke.md). `gupkg pkg check PATH
--install` uses the same machinery with a throw-away root. A sandbox whose `system.json` says `"elevated": true` makes the run a system-mode run, which refuses
`--settings`.

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
2. `gupkg logs NAME --path` and read the newest log; the receipt (`<name>\<version>\App.info`) describes the App only and holds no log.
3. For a registry problem retry `gupkg registry sync`; a failed sync keeps the previous validated cache, and `--offline` uses it.
4. For a package problem fix `pkg.toml`, run `gupkg pkg check <path>`, then `gupkg install NAME`.
5. For gupkg itself run `gupkg self status`, then re-run the bootstrap if files differ.

A failing package never invalidates the others in a batch, and a rerun revalidates what is already current.

## Release and maintenance (development tree)

Every routine job has a script in `scripts\`. None of them ships in the release ZIP. They run on the repository's `.venv` only and
never search for another Python; a missing `.venv` is created first by `setup.cmd`.

| Script | What it does |
|---|---|
| `setup.cmd [python.exe] [--force]` | Create or repair `.venv`: it must have pip and the packages of `requirements-dev.txt`. Tries `uv venv --seed` first, then `python -m venv`. See "The development environment" below. |
| `test.cmd [pytest args]` | Run the test suite. |
| `run.cmd [gupkg args]` | Run `src\gupkg\gupkg.cmd` from the source tree. |
| `bump.cmd [patch\|minor\|major\|<version>]` | Raise the patch number (the default), the minor or the major number, or set an exact version, in `_version.py`, and commit. |
| `release.cmd ["notes"] [--no-push]` | Test, build the ZIP, tag, push the tag, upload the ZIP and its `.sha256` file to GitHub release `v<version>`, download the ZIP again and require the same SHA-256, add the `RELEASES.md` row (with that SHA-256), and push the branch. Without notes the row says `gupkg <version>`. `--no-push` keeps everything local and uploads nothing. |
| `check-release.cmd [version]` | Download the published release from the address the bootstrap uses and check it against the `RELEASES.md` row. Prints PASS or FAIL per check; exit 1 on any FAIL. Changes nothing. |
| `build-standalone.cmd [output.zip]` | Build the release ZIP in `dist\` and print its SHA-256. |
| `fetch-runtime.cmd [--destination DIR] [--force]` | Fetch the pinned embeddable CPython, verify it, write `._pth` and install the locked dependencies. |

Dependency maintenance runs the tools directly: `python tools\update_deps.py` and `python tools\check_deps.py`.

### The development environment

`setup.cmd` is the one command that makes the environment work, and every other script runs it for you when `.venv` is missing.

- **No `.venv`, or a `.venv` without pip** (for example one made by `uv venv` without `--seed`): it is removed and created again. uv is
  tried first with `uv venv --seed`, which includes pip; if uv is not installed or fails, `python -m venv` is used. Pass
  `scripts\setup.cmd C:\path\python.exe` to choose the interpreter (any Python 3.11 or newer; without it uv picks one and the
  fallback uses `python` from `PATH`).
- **A good `.venv`**: `requirements-dev.txt` is installed again, which brings the packages to the pinned versions.
- **`--force`** removes and recreates it even if it looks good. Close programs that use `.venv` first, or the removal fails.

pip must be in `.venv` because the build installs the locked wheels into the release bundle with it (`--require-hashes`); the
bundle itself never contains pip, and gupkg never installs anything at run time. The packages in `requirements-dev.txt` match
`tools\gupkg-deps.lock`, so tests import the versions that the release ships.

### The release workflow

A release is three commands, run from a clean working tree on `main`, with the GitHub CLI signed in (`gh auth status`):

```bat
scripts\bump.cmd
scripts\release.cmd "one-line notes"
scripts\check-release.cmd
```

1. **`bump`** edits the single version line in `src\gupkg\_version.py` and commits it (`Bump version to <version>`). With no argument
   the patch number goes up (1.0.0 to 1.0.1); `minor` and `major` raise those parts and reset the lower ones; an exact version such as
   `1.2.3` is also accepted. Only plain dotted numbers are allowed. It builds nothing and pushes nothing.
2. **`release`** does these steps in order and stops at the first failure:
   1. runs the whole test suite;
   2. refuses a dirty tree and a version that already has a row in `RELEASES.md`;
   3. builds `dist\gupkg-<version>-win-amd64.zip` from `HEAD` (see `build-standalone.cmd`) and checks that its `RELEASE.txt` names
      this version and this commit;
   4. computes the ZIP's SHA-256 and writes `dist\gupkg-<version>-win-amd64.zip.sha256` (`<digest>  <file name>`);
   5. tags the released commit `v<version>` and pushes the tag;
   6. creates GitHub release `v<version>` with the ZIP and the `.sha256` file as assets;
   7. downloads the ZIP from the release and requires the same SHA-256, so the published bytes are the ones you built;
   8. only now adds the row to `RELEASES.md` (version, released commit, date, SHA-256, notes) and commits it, so the table never
      advertises a release that was not published and verified;
   9. pushes the branch (and with it the row) to the first Git remote.
3. **`check-release`** is the independent confirmation. It does not trust anything `release` printed: it downloads the ZIP the way the
   bootstrap does and reports one PASS or FAIL line for each of these:
   - `RELEASES.md` has a row for the version, with a commit and a SHA-256;
   - the local tag and the tag on the remote point at the released commit;
   - the remote `main` contains your `HEAD`, so the row is pushed;
   - the downloaded ZIP has the SHA-256 of the row;
   - the `.sha256` asset names the same SHA-256;
   - `RELEASE.txt` inside the ZIP names this version and this commit;
   - every file listed in `MANIFEST.sha256` exists with that SHA-256, and the tree holds no file the manifest does not list.

   Run it again at any time, for any version (`scripts\check-release.cmd 1.0.0`); it changes nothing.

Which SHA-256 protects what:

| Digest | Where it is | Computed | Checked by |
|---|---|---|---|
| ZIP SHA-256 | `RELEASES.md` row, the `.sha256` asset | at release time, from the built ZIP | `release` (after the upload), `check-release`, the bootstrap and gupkg's own tracker before anything is unpacked |
| `MANIFEST.sha256` entries | inside the ZIP, `gupkg\MANIFEST.sha256` | by the build, for every file of the bundled tree | `check-release`, gupkg's install and `self` health checks |
| Pinned runtime and wheel digests | `tools\fetch_runtime.py`, `tools\gupkg-deps.lock` | by the maintainer when updating dependencies | the build (`--require-hashes`) |
| Commit hash | `RELEASES.md` row, the tag, `RELEASE.txt` | by Git | `check-release` compares all three |

If a step fails:

- **Tests or build fail:** nothing was tagged or published; fix the cause and run `release` again.
- **`gh` fails after the tag was pushed (step 6):** the tag exists but no release and no row. If nobody has downloaded anything, undo with
  `git tag -d v<version>` and `git push <remote> :refs/tags/v<version>`, fix `gh` (`gh auth login`), and run `release` again.
- **The verification download (step 7) differs:** the release exists but no row was recorded. Remove it with
  `gh release delete v<version> --cleanup-tag`, delete the local tag with `git tag -d v<version>`, and run `release` again.
- **The final push (step 9) fails:** the release is already public and the row is committed locally; run `git push <remote> HEAD`, then
  `check-release`.
- **`release --no-push` rehearsal:** it builds, tags and commits the row locally and uploads nothing. Undo it with
  `git tag -d v<version>` and `git reset --hard HEAD~1`.
- Never delete or move a tag whose release people may have downloaded; publish a new version instead.

`RELEASES.md` is the release registry: a table of version, full commit hash, date, SHA-256 and notes, newest first. gupkg's own tracker
(`pkg.local\track.py`) reads the table from `main`, and the ZIP it names must be attached to GitHub release `v<version>`.

Before a release `release` runs the tests itself. The Windows-only checks (UAC, the Start Menu, the `PATH` broadcast, a real GitHub
release) are the numbered entries of [`tests/manual_smoke.md`](../tests/manual_smoke.md).

## The registry repository

The packages live in their own repository, `gupkg-registry`, checked out beside this one (ignored here). Changing a package needs
no gupkg release: commit and push there, and the next `gupkg registry sync` picks it up. Its CI runs `gupkg pkg check <path> --json`
and the registry tool's `validate`. Top-level folders starting with `_` or `.` are repository furniture, never packages.
