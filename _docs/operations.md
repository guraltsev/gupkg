# Operations guide

This is the operational guide for the standalone Windows distribution and the
central manager. The top-level [README](../README.md) explains the package
model; this page is the runbook for installing, configuring, repairing, and
operating `gupkg`.

## Choose an operating mode

Use package-local mode when you already have a package directory and want to
install or repair that one package. Use manager mode when one centrally
installed `gupkg` should discover packages in separate user and system roots.

| Need                                         | Entry point                                           |
| -------------------------------------------- | :---------------------------------------------------- |
| Install gupkg itself and set up manager mode | `gupkg-bootstrap.cmd [-Scope auto|user|system]`       |
| Create the manager configuration             | `gupkg manager init`                                  |
| Install or repair one package                | `gupkg install [PATH]`, `gupkg config-fix [PATH]`     |
| Inspect or update one package                | `gupkg config-check [PATH]`, `gupkg update [PATH]`    |
| Inspect all managed packages                 | `gupkg manager list`, `gupkg manager doctor`          |
| Check or apply managed updates               | `gupkg manager update [--check-only|--download-only]` |
| Find packages in the registry                | `gupkg manager registry sync`, `gupkg manager search` |
| Install a registry selector                  | `gupkg --scope user|system manager install SELECTOR`  |
| Open an interactive interface                | `gupkg tui [PATH]`, `gupkg manager tui`               |

Every invocation names its command explicitly. Package paths are optional only
when the current directory resolves as a package. Manager selection never
occurs implicitly from the current directory.

## Standalone installation

`gupkg-bootstrap.ps1` (with its `gupkg-bootstrap.cmd` launcher, which only
bypasses the script execution policy for one run) is the supported entry point.
It runs on stock Windows PowerShell 5.1:

```bat
gupkg-bootstrap.cmd                      :: auto: system scope if elevated, otherwise user
gupkg-bootstrap.cmd -Scope user          :: force per-user, even when elevated
gupkg-bootstrap.cmd -Scope system        :: force all users, elevated shell required
gupkg-bootstrap.cmd -Source \\server\share\gupkg-0.18.0-win-amd64.zip -Sha256 <digest>
```

What it does, in order: choose the root (`%USERPROFILE%\opt` or `C:\opt`, or
`-Root`); fetch the release ZIP from `-Source` (a URL, ZIP, or extracted folder;
default: the newest row of `RELEASES.md` of the official repository, whose asset
URL is derived from the version) and verify the download against `-Sha256` (for
the default source, the digest in that same `RELEASES.md` row; a `-Source` ZIP
without `-Sha256` prints a warning); refuse a payload without
`gupkg\python\python.exe`; check every file against `gupkg\MANIFEST.sha256` and
stop on any modified, missing or extra file; place it at `<root>\gupkg\<version>`,
reusing a matching folder unless `-Force`; run `gupkg install` on it, which
creates the `current` junction, the `gupkg` and `gupkg-tui` commands, and the
PATH entry; and run `gupkg manager init` unless `-SkipManagerInit` (an existing
configuration is left untouched). Every step is repeatable. It needs no Python in
advance, and the only downloads are `RELEASES.md` and the ZIP. Keep the version
directory immutable; repairs and upgrades replace or activate a complete version
rather than editing the runtime in place.

There is one distribution: the release ZIP `gupkg-<version>-win-amd64.zip`. It
contains a version directory with the embedded CPython, the hash-locked
dependencies, the native shims, the application, package metadata, the update
hooks, `gupkg\RELEASE.txt` (version and commit) and `gupkg\MANIFEST.sha256` (one
`<sha256>  <relative path>` line for every other file, sorted by path). It ships
no manager configuration: roots and registry settings belong to the
administrator and are created by `manager init`.

The ZIP and every digest that verifies it are produced by the build, never by
installed code:

```bat
scripts\build-standalone.cmd              :: dist\gupkg-<version>-win-amd64.zip, SHA-256 printed
```

The build (`tools\build_standalone.py`) verifies the CPython archive against the
digest pinned in `tools\fetch_runtime.py` (the only place the URL and digest
live), installs the locked wheels with `--require-hashes`, assembles the tree,
writes `RELEASE.txt` and then `MANIFEST.sha256`, zips it, and prints and saves
(`<zip>.sha256`) the ZIP's SHA-256. `tools\release.py` uploads the ZIP as the
asset of GitHub release `v<version>` and records that digest in `RELEASES.md`.
`MANIFEST.sha256` is protected in transit by the ZIP digest in `RELEASES.md`, and
at rest by the admin-only folder permissions that a system-scope install sets;
signing it is out of scope.

For a source checkout, the `src\gupkg.cmd` launcher remains available for
development. It selects a packaged `gupkg\gupkg.exe` when present and otherwise
delegates to the adjacent `gupkg\gupkg.cmd`, which runs the committed
`src\gupkg\python` like any other install: there is no system Python, `PATH`
lookup, `GUPKG_PYTHON` override or download, not even for development.

## Manager configuration

Manager mode is enabled by a 2 `gupkg-config.toml`. It is read from:

1. an explicit `--config PATH`; or
2. the file beside the imported `gupkg.cli` module (the gupkg package folder).

The configuration chooses package roots, executable folders and the registry
source, and an elevated run acts on all of them, so nothing user-writable is
searched: not the current directory or its parents, not `%GUPKG_HOME%`, and not
`%APPDATA%`. An elevated run also refuses any configuration (including one given
with `--config`) whose folder is not admin-only. `gupkg manager init` writes the
file into the package folder, and `gupkg update` carries it into the new
version. A configuration left in `%APPDATA%\gupkg` by an older version is
ignored; review it and copy it into the package folder if you still want it.

The `gupkg-tui` launcher is a convenience wrapper for the package `tui`
command; it does not select manager mode implicitly. Use `gupkg manager tui`
when manager mode is intended. Without a package path, package `tui` requires
the current directory to resolve as a package.

Use this shape for a new manager configuration:

```toml
mode = "manager"
schema_version = 2

[packages]
system = 'C:\opt'
user = '%USERPROFILE%\opt'

[bin]
system = 'C:\bin'
user = '%USERPROFILE%\bin'

# Optional. These are the defaults; change them to move the cache or use a mirror.
[registry]
cache = 'C:\opt\gupkg\current\App\registry'
source = "https://github.com/guraltsev/gupkg-registry.git"
```

`packages.system` and `packages.user` are required, distinct, non-nesting
collection roots. The two `bin` directories must also be distinct. The whole
`[registry]` table is optional: `cache` defaults to
`App\registry` inside the running gupkg version (so an administrator's install keeps
it in the same admin-only tree as the code that trusts it). It must be outside
both package roots, except inside a package version's `App` folder, and
`source` defaults to the official `gupkg-registry` repository. It may instead
be the URL of any ZIP archive whose single top-level folder holds the packages
(an `https://` mirror or a `file:` share). Existing configured paths must be
directories; loading the configuration does not create roots. Relative paths
are resolved relative to the configuration file, `%NAME%` references use the
case-insensitive process environment, and a leading `~` means the current
user's home directory. Unknown variables, shell substitutions, and unsafe path
relationships are rejected.

Only schema version 2 is accepted. Older files must be converted as a separate
administrative operation before they can select manager mode.

## First-time setup

```bat
gupkg manager init            :: write gupkg-config.toml with defaults and create its folders
gupkg manager registry sync   :: download the package catalogue
gupkg manager doctor          :: confirm everything is healthy
```

`manager init` refuses to overwrite an existing configuration unless you pass
`--force`, and reports (without failing) any folder it could not create, such as
the system root without Administrator rights. Edit the file afterwards to move
any location.

## Registry workflow

The official registry is the separate repository
[`gupkg-registry`](https://github.com/guraltsev/gupkg-registry): one folder per package at its top level.
`gupkg manager registry sync` clones it (shallow, HTTPS only, no hooks, user Git
settings ignored) when a `git.exe` in a folder that only administrators can
modify is on `PATH`; a user-writable Git is never run. Otherwise it downloads the
repository's GitHub archive with Python's standard library. Registry data is
treated as untrusted metadata: sync validates manifests and selectors with
path-safety checks, publishes no Git metadata, and does not import or execute
package code. The cache keeps each registry in its own folder
(`<cache>\gupkg-registry`, with room for other registries later); the active
revision is the commit ID, and a failed sync leaves the previous validated tree
in place.

```bat
gupkg manager registry sync
gupkg manager registry status
gupkg manager search ripgrep
gupkg manager search --offline
```

Use `--offline` with `search` when network access is forbidden. Offline search
uses only the last validated cache. `registry status` reports cache and active
tree state without downloading.

Install a registry package by its selector:

```bat
gupkg --scope user manager install ripgrep
gupkg --scope user manager install vscode --offline
```

The selector resolves to a validated manifest, then the normal package install
workflow applies its origin and integration declarations. `--offline` allows
use of the registry cache but does not make a missing application payload
available: an origin still needs its own local files or cached/offline-capable
source. Local package paths never contact the registry.

## Self-health and recovery

The self commands inspect the bundled runtime, the installed files, and the
scope-specific shim/configuration pair:

```bat
gupkg manager self status
gupkg --scope user manager self repair
gupkg --scope system manager self repair
gupkg --scope user manager self update
```

`manager self status` is read-only and report-only. It checks every file of the
running version against its `MANIFEST.sha256` and lists the modified, missing and
extra files (`gupkg\gupkg-config.toml`, `App`, `Icons`, `Shortcuts` and
`__pycache__` folders are installation state, not release content). Exit status
`3` means the install is damaged. `manager self repair` restores missing or
inconsistent launch metadata and shims from the installed version. `manager self
update` finds the newest row of `RELEASES.md`, downloads that release ZIP,
verifies the ZIP against the row's SHA-256 and the unpacked tree against its own
manifest, and activates the new version with its own `python\`; nothing of the
previous runtime is carried over, and a failed check leaves the active version
unchanged. When the newest release is major version 1 or later, `self update`
installs nothing and names the bootstrap command to run instead. System scope
requires elevation.

A missing or damaged runtime is an installation error with one fix: install the
release ZIP again with `gupkg-bootstrap.cmd` (`-Source` with a ZIP and `-Sha256`
for a vetted copy, `-Force` to replace the folder). gupkg does not download or
repair a runtime itself.

## Routine recovery

When a manager command fails, preserve the evidence before changing anything:

1. Run `gupkg manager registry status` and `gupkg manager doctor`.
2. Run `gupkg --format toml manager list` to capture scoped inventory and health fields.
3. For a registry problem, retry `gupkg manager registry sync`; if it fails, continue
   using the previous validated cache or add `--offline`.
4. For a package problem, fix the package root or manifest, then run
   `gupkg config-check <path>` and `gupkg install <path>`.
5. For a standalone problem, run `gupkg manager self status`, then the appropriate
   scoped `manager self repair`.

Manager update planning is non-mutating. Use `gupkg --scope auto manager update
--check-only` before a confirmed full update; a failed target does not invalidate successful targets,
and a later safe rerun revalidates already-current packages. Declining mixed
scope elevation must leave user packages unchanged.

## Elevated runs

An elevated (Administrator) `gupkg` executes only code that non-administrators
cannot modify. `gupkg.cmd` is the same for every run; `bootstrap.py` detects
elevation itself (the process token) and then:

- requires the bundled `python\python.exe` in isolated mode (`-I -s -B -S`), as for
  every run (an ordinary run is refused too on any other interpreter, so `python -m
  gupkg` works only on the bundled Python; use `scripts\run.cmd` in a checkout);
  `GUPKG_PYTHON`, `gupkg.python`, `python`, `py`, and `PYTHONPATH` are never consulted;
- verifies that the version folder, the package folder, the runtime and its
  `Lib\site-packages`, and every directory on the import path are writable by
  Administrators and SYSTEM only, and refuses otherwise, printing the offending
  principal and an `icacls` command;
- never runs `pip` and never downloads a runtime, in any run.

`gupkg-bootstrap.ps1 -Scope system` installs under `%SystemDrive%\opt` and
restricts `opt\gupkg` accordingly. For any other location, run the printed
`icacls` command from an elevated prompt. A folder created under the root of
`C:` is writable by Authenticated Users until it is restricted.

The dependencies are installed by the build, from the hash-pinned
`tools\gupkg-deps.lock`, and ship inside the release ZIP. An `ImportError` in
gupkg itself therefore means a damaged install, and `manager self status` finds
the file. Dependency freshness is a maintainer concern (see the development
guide): `tools\check_deps.py` compares the pins with PyPI and
`tools\update_deps.py` updates the lock and the committed runtime. Package-local
update hooks run once and may import only the standard library, what the bundle
ships and modules in their own `pkg.local` folder; a missing import fails the
update with the module's name. The earlier design is recorded in
`docs/elevated-runtime-design.md`.

### Portable roots are never machine-wide

The 1.0 install modes (design section 6) include `portable`, where an application's
settings and state live in `<package>\data` so the package folder can be copied or
carried. A portable root is never the system root and never lies inside it (and the
system root never lies inside a portable root). The reason is the threat model of
section 9.6: state that a package keeps inside its own folder is writable by whoever runs
the package, and in the admin-only system root that would be shared, writable state
next to code that an elevated gupkg runs. An elevated gupkg also never acts on a user or
portable root: use a normal shell for those. A gupkg that
runs from a user-writable place never relaunches itself elevated: system installs need the
system gupkg.

## Maintenance scripts

Every routine job has a script in `scripts\`, so no Python needs to be typed.
They use the repository's `.venv` only and never search for another Python; without
it they exit 9009 and print how to create it
(`<python> -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt`;
`python -m venv` puts pip into the `.venv`).
`scripts\run.cmd` runs `src\gupkg\gupkg.cmd`, so a manual test uses the bundled
Python like a user does. The committed runtime has no pip, and neither does any
release: the build scripts and `tools\update_deps.py` need the pip of the
repository's `.venv`. These are development-tree tools: none ships in the
release ZIP and gupkg cannot reach them.

| Script | What it does |
|---|---|
| `test.cmd [pytest args]` | Run the test suite. |
| `run.cmd [gupkg args]` | Run `src\gupkg\gupkg.cmd` from the source tree. |
| `bump.cmd <version>` | Set the version in `_version.py` and both `pkg.toml` files and commit. |
| `release.cmd "notes" [--no-push]` | Test, build the ZIP, tag, upload it to GitHub release `v<version>`, add the `RELEASES.md` row (with the ZIP's SHA-256), and push the branch. `--no-push` keeps everything local. |
| `build-standalone.cmd [output.zip]` | Build the release ZIP in `dist\` with the pinned, verified Python and the locked dependencies, and print its SHA-256. |
| `fetch-runtime.cmd [--destination DIR] [--force]` | Fetch the pinned embeddable CPython, verify it, write `._pth` and install the locked dependencies (default destination: the committed `src\gupkg\python`). |

Dependency maintenance runs the tools directly: `python tools\update_deps.py`
(resolve newer releases, show the plan, ask; `--yes`, `--repair`) and
`python tools\check_deps.py` (report newer releases; exit 1 when there are any).

## Releasing gupkg itself

`RELEASES.md` is the release registry: a table of version, full commit hash,
date, SHA-256 and notes, newest first. The SHA-256 is that of the release ZIP
`gupkg-<version>-win-amd64.zip`, attached to the GitHub release `v<version>`.
gupkg's own `pkg.local/check_update.py` reads the table from `main`, so
`gupkg manager self update` finds the newest row, downloads the asset, and has
its normal download verification compare it with that digest; then
`unpack_app.py` checks the unpacked tree against `MANIFEST.sha256` and the
recorded commit before the new `<version>` folder is committed. A row without a
digest (written before ZIPs had digests) is an error for the updater, and
`gupkg-bootstrap.ps1` installs the newest row on a fresh machine the same way.

To release (from a clean, committed tree, with the GitHub CLI `gh` signed in):

```bat
scripts\bump.cmd 0.14.0
scripts\release.cmd "one-line notes"
```

`scripts\release.cmd` runs the tests, then `tools\release.py`, then pushes the
branch to the first Git remote (`--no-push` keeps everything local and uploads
nothing). `tools\release.py` refuses a dirty tree, a version that is already
listed, and a ZIP that was not built from `HEAD` (the commit is recorded in the
ZIP as `gupkg\RELEASE.txt`). It builds the ZIP, tags the released commit
`v<version>`, pushes the tag, creates the GitHub release with the ZIP as its
asset, and commits the new row naming `HEAD` as the release and the ZIP's
SHA-256. The row is committed after the upload, so `RELEASES.md` never advertises
a release that was not published; the branch push makes it visible.

## Release and validation checks

Before releasing gupkg or a standalone bundle, run:

```bat
scripts\test.cmd
```

(Registry changes are checked in the registry repository; see below.) The test
suite covers manager schema/discovery, registry parsing
and cache behavior, standalone assembly, payload variables, and the existing
package/update workflows. The Windows-specific cases in
`tests\manual_smoke.md` still require a real Windows environment.

## The registry repository

The packages live in their own repository, `gupkg-registry`, checked out beside
this one as `gupkg-registry\` (ignored by this repository). Changing a package
needs no gupkg version bump or release: commit and push in that repository and
the next `gupkg manager registry sync` picks it up.

Everything that checks package consistency lives in that repository, not here:
its tests (manifests, per-package update hooks, and that every package ships the
same `gupkg-tui.cmd` as `src\package-tui.cmd`), `_scripts\test.cmd`,
`_scripts\validate.cmd`, and `_scripts\sync-launchers.cmd` for refreshing the
launcher copies after `src\package-tui.cmd` changes. `gupkg-tui.cmd` opens gupkg's
text interface for its package (a gupkg beside it, else `gupkg.exe` on `PATH`), so
installing a package needs no command line. They use this repository's code,
found beside the registry checkout or in `GUPKG_REPO`. Top-level folders whose
names start with `_` or `.` are repository furniture and never packages.

