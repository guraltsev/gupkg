# gupkg

**Install and update Windows programs from a small text file, with nothing hidden.**

`gupkg` manages self-contained applications that live in ordinary folders. You
describe a program once in a short `pkg.toml`; `gupkg` downloads and verifies
it, then creates the Start Menu shortcuts, environment variables, PATH entries,
and command-line wrappers it needs. Every version stays side by side, updates
are staged before they switch over, and any command can be repeated safely to
repair a machine.

```bat
gupkg install pandoc                   :: download, verify, install, wire up Windows
gupkg check-updates pandoc             :: look for a new release and prepare it; nothing is activated
gupkg update pandoc                    :: activate the newest prepared release; the old one stays
gupkg switch pandoc 3.6.1              :: roll back
gupkg manager update --all --yes       :: the same for every package you manage (starts gupkg-manager)
```

**Why use it**

- **Minimal friction.** One folder per program, one readable file per folder.
  No installers to click through, no registry archaeology.
- **Safe by construction.** Downloads are checksummed and built in a separate
  folder; the working version is never modified in place. A failed update
  changes nothing.
- **Repairable.** `install` re-applies shortcuts, PATH, and wrappers without
  re-downloading. `check` finds drift and `check --fix` repairs it.
- **Built for administration.** `--plan` shows what a call would do, `--yes` and
  `--no-input` for unattended runs, stable exit codes, and `--json` for
  scripts. A failing package never blocks the others.
- **Transparent.** Plain files and folders, an NTFS junction, standard
  registry values. Nothing runs in the background.

## Documentation

| I want to... | Read |
| --- | --- |
| Install my first program in five minutes | [Getting started](_docs/getting_started.md) |
| Do a specific task (nightly updates, rollback, CI checks, ...) | [Cookbook](_docs/cookbook.md) |
| Fix an error message | [Troubleshooting](_docs/troubleshooting.md) |
| Set up and run the multi-package manager, registry, self-repair | [Operations guide](_docs/operations.md) |
| Look up every `pkg.toml` field | [`pkg.toml` reference](#pkgtoml-reference) below |
| Look up every command and option | `gupkg --help` and `gupkg <command> --help`, or [Commands and options](#commands-and-options) below |
| Understand or change the code | [Development guide](_docs/development_guide.md) |

Requires Windows. gupkg ships as one self-contained release ZIP that includes its
own Python, so no Python needs to be installed (and none is ever used besides
the bundled one). Git is needed only for Git-based packages; network access only
when a package contacts its source.

## Installing gupkg

`gupkg` is a self-contained tool that is deployed as a folder; it is not distributed as a
package on a Python package index. Pick the route that fits:

- **Bootstrap script (recommended).** Download `gupkg-bootstrap.cmd` from this
  repository and run it. If `gupkg-bootstrap.ps1` is not beside it, the command
  shows the address and asks before downloading the script from the official
  repository (set `GUPKG_BOOTSTRAP_YES=1` to skip the question, or
  `GUPKG_BOOTSTRAP_URL` to use another address):

  ```bat
  gupkg-bootstrap.cmd                  :: auto: all users if elevated, otherwise just you
  gupkg-bootstrap.cmd -Scope user      :: force per-user, even from an elevated shell
  gupkg-bootstrap.cmd -Scope system    :: force all users (needs an elevated shell)
  ```

  It downloads the newest release ZIP (`gupkg-<version>-win-amd64.zip`, which
  `RELEASES.md` lists with its SHA-256) and checks the download against that
  digest. It then checks every file of the unpacked release against the
  release's `MANIFEST.sha256` (a modified, missing or extra file stops the
  install), places it at `<root>\gupkg\<version>` (default root
  `%USERPROFILE%\opt` for user scope, `C:\opt` for system scope; `-Root` changes
  it), creates the `gupkg` and `gupkg-tui` commands and their PATH entry, and
  runs `gupkg manager init`. It is safe to repeat. The only downloads are
  `RELEASES.md` and the ZIP. Use `-Source` with a ZIP (URL or path) or an
  extracted release folder, and `-Sha256` to verify it, to install a vetted
  release from your own share; `-SkipInstall` only places the files.
  Nothing needs to be installed first: the ZIP carries its own Python, the
  locked dependencies and the native launchers.
- **Release ZIP by hand.** The ZIP built by `tools\build_standalone.py` (and
  attached to each GitHub release) is the only distribution. Give it to the
  bootstrap script as `-Source`, or unpack it yourself and run its
  `gupkg\gupkg.cmd install <folder> --mode user`.
- **From a source checkout (development).** `src\gupkg.cmd install PATH` runs
  on the Python committed at `src\gupkg\python`, like every other launch: no
  system Python, `PATH` lookup or download is ever used. Fetching or refreshing
  that runtime, updating the locked dependencies and building the release ZIP
  are maintainer tasks that live in `tools\` and `scripts\` (see the
  [development guide](_docs/development_guide.md)); none of them ships.

If a release is damaged or its Python is missing, gupkg does not repair it at
run time. The fix is always the same: install the release ZIP again
(`gupkg-bootstrap.cmd`).

Then open a new terminal and check it with `gupkg --version`. The [operations guide](_docs/operations.md)
covers release build inputs, manager setup, the registry, and recovery commands,
and [Getting started](_docs/getting_started.md) installs a first program.

## What gupkg manages

`gupkg` manages self-contained Windows applications that live on disk rather
than in a central package store. A package author puts an application's files,
its version number, and a small `pkg.toml` definition in one directory. From
that definition, `gupkg` selects the active version and makes the application
available to Windows: it can create Start Menu shortcuts, set environment
variables, add directories to PATH, and generate command-line wrappers. It can
also fetch application files when they are not already present and stage new
versions when updates are available.

The model is deliberately repair-friendly. Re-running an install re-applies
the declared Windows integration without re-downloading an application whose
files are already present.

## At a glance

- Works on Windows; the release ZIP carries its own Python.
- Treats the directory layout as the authority for package identity.
- Uses a canonical, strict `pkg.toml` format.
- Supports ZIP, Git, and package-local-script application sources.
- Checks and stages updates before activating a new immutable version.
- Supports GitHub Releases and trusted package-local Python update hooks.

Git is needed only by packages that declare a Git origin or Git updates; gupkg itself, including
the package registry, downloads plain ZIP files with Python's standard library. Network access is
required only when a configured origin or update source is contacted.

## Architecture and organization

Each application has a **package root**. It contains one or more immutable
**version directories** and a `current` junction that points to the active
one. The version directory is the unit that `gupkg` installs and updates.

```text
<PackageName>/
  current/                         # NTFS junction to the active version
  1.2.3/
    App/                            # application payload
    Icons/                          # optional icon assets
    Shortcuts/                      # optional package-owned assets
    pkg.local/                      # trusted update hook modules
    pkg.toml
```

`App` is the application payload: the files that actually run. For example,
`App` might contain `rg.exe`, a portable editor's executable and libraries, or
a Git checkout. `gupkg` never invents a particular application layout inside
`App`; it either starts with the files already there or populates them from the
configured origin. Shortcuts, PATH entries, environment values, and wrappers
normally point at files or directories beneath `App`.

`pkg.toml` sits beside `App` and describes the package version. It declares
how to obtain the payload when necessary and how to expose it to Windows. The
configuration uses package variables so that it does not need hard-coded
machine-specific paths: `$App`, `$VersionRoot`, `$Icons`, and
`$Shortcuts` resolve to matching directories in the installed version, while
`${version}` resolves to the version in the directory name. For example,

```toml
[[shortcut]]
name = "Ripgrep"
targetPath = "$App\\rg.exe"

[[environment]]
Name = "RIPGREP_HOME"
Value = "$App"

[[bin]]
name = "rg"
target = "$App\\rg.exe"
```

This creates a shortcut to the executable, stores the full `App` path in an
environment variable, and creates a command wrapper that launches the same
executable. When a package needs a sibling directory, it should write that path
explicitly beneath `$VersionRoot`. The exact expansion rules are documented in
[Variables and expansion](#variables-and-expansion).

`Icons` and `Shortcuts` are optional package-owned asset directories. `Icons`
is a natural place for shortcut icons; `Shortcuts` is available to package
scripts or configuration that needs package-local shortcut assets. `pkg.local`
is reserved for trusted Python update-check and unpack hooks. `.gupkg`, created
at the package root by update operations, holds manager state, locks, receipts,
and disposable work files rather than application files.

A version directory is named by its version string, which may be any text that
is a valid folder name (`1.2.3`, `2024-05`, `nightly`). Text after the first
`.local.` is the package-local revision, itself any string: `1.2.3.local.000002`
has upstream version `1.2.3` and local revision `000002`. Versions are ordered by
dotted segments, numbers numerically and text alphabetically, and a version
without a local revision sorts before the same version with one. A folder counts
as a version when it contains `pkg.toml`; `current`, `_bootstrap`, and names
starting with `.` are reserved. `_bootstrap` is a package's update template;
promoting it creates the first real version directory. The version is the folder
name only and is never repeated in `pkg.toml`.
The package name is the package-root directory name. A name ending in
`-portable` is portable-only by convention.

You may give `gupkg` a version directory, a package root, or its `current`
junction. A package root without `current` is accepted only when it contains
exactly one version directory. Update checks and downloads accept any version
directory, so a historical definition can provide the update configuration.
Activation refuses a downloaded version when a newer installed version exists.

Installing a newer version advances `current`. Installing an older version
does not replace a newer active version unless `--allow-downgrade` is supplied. Old
versions are retained.

## Commands and options

One grammar serves people, scripts and the UI programs. Options go **after** the command; the only options
before it are `--version`, `--help`, `--settings PATH` and `--sandbox DIR`. A target is a package name
(`firefox`, `firefox@135.0`, `user:firefox`, `main/firefox`) or a path (anything with a `\` or `/`, or starting with
`.`). Several targets in one call run one after another and the run continues past a failure. The command line has
no `--all` and no interactive screen: `gupkg-manager` is the program for batches and interactive use.

```text
gupkg check-updates <target>... [--no-download] [--mode M] [--skip-checksum]   run trackers; write and prepare new versions; activate nothing
gupkg install  <target>... [--mode M] [--skip-checksum] [--refresh] [--in-place] [--yes]
gupkg update   <target>... [--mode M] [--skip-checksum] [--yes]   activate the newest KNOWN version; never runs a tracker
gupkg switch   <target> <version>                       activate another local version (rollback)
gupkg uninstall <target>... [--purge] [--yes]           remove the integration; --purge also deletes the package folder and its data
gupkg check    [<target>...] [--fix]                    health: drift, App fingerprint, registry fingerprint, unverified installs
gupkg list     [--mode M] [--installed|--available|--outdated|--broken|--local] [--details] [--names]
gupkg info     <target>...
gupkg search   <query> [--registry R] [--offline]
gupkg cleanup  [<target>...] [--keep N] [--bundled]     old Apps, receipts, work folders, registry trees, logs (only when asked)
gupkg link     <target> [<registry>/<name>] [--unlink]  follow another registry entry, the same-name entry again, or none
gupkg logs     [<target>] [--open | --path]
gupkg shell    [--mode M]                               the PATH line of a root with integrate = "none"
gupkg registry sync|status|list|add|remove
gupkg settings show|path|init|set KEY VALUE
gupkg pkg new|check|pin|bundle|export
gupkg self status|repair|check-updates|update
gupkg tui <target> | gupkg manager ... | gupkg migrate ...     start gupkg-tui, gupkg-manager, gupkg-migrate of this gupkg
```

Checking and installing are separate operations. `check-updates` runs a package's tracker and writes the new
version as a pinned directory (`<root>\<name>\<version>\`), by default with its `App` downloaded, verified and
unpacked; it never activates anything. `update` then activates the newest version that is already known, locally or in
the followed registry, and prepares its `App` first if it has none. `install` and `update` never run a tracker, so
`update` of a package with nothing newer says "up to date" and makes no request.

Every command takes `--json`. Every mutating command takes `--yes`. The mode comes from `--mode system|user|portable`,
else from the package's install record or the root that holds it, else from `default_mode` in the settings (`auto`: an
elevated shell installs in system mode, any other in user mode). A call that needs administrator rights relaunches
gupkg **once** elevated, after one question, and only when this gupkg itself lives in an admin-only folder. The call then
waits for the elevated child and exits with the child's exit code (`relaunched: true` in the document); a target or option
value holding `& | < > ^ " % ! `` ` ``, a control character or a lone surrogate is refused (exit code 2) before anything is asked,
because it would travel on the elevated command line. Scheduled callers pass a unique `--events-file NAME` and treat a missing
`run_end` line as a failure; a call with several targets needs `--yes` under `--no-input` (the question "run this on N
packages?" is exit code 5 otherwise).
Redirected output is UTF-8 and the `--json` document is pure ASCII, whatever the code page. The child's exit code is clamped to
gupkg's codes (`0`-`6`, anything else is `4`; the raw code is `child_raw_exit_code`). If the user elevates through a different
administrator account, the child's temporary folder differs, so Ctrl-C cannot stop it: the call waits 30 seconds and gives up
with exit code 5 (the child's own `stop_file` is in its events file).

### Install modes

| Mode | Root (default) | Start Menu | Environment and `PATH` | Shims |
| --- | --- | --- | --- | --- |
| `user` | `%USERPROFILE%\opt` | `%APPDATA%\Microsoft\Windows\Start Menu\opt` | `HKCU\Environment` | `%USERPROFILE%\bin` |
| `system` | `C:\opt` (admin-only) | `%ProgramData%\Microsoft\Windows\Start Menu\opt` | `HKLM` environment | `C:\bin` |
| `portable` | `[roots].portable` | the user's, unless `integrate = "none"` | the user's, unless `integrate = "none"` | `<root>\bin` |

`gupkg install firefox --mode user` and `gupkg install %USERPROFILE%\opt\firefox` put the same shortcuts, shims and
`PATH` entries in place, because both use the context built from the settings. A folder outside every root (an unzipped
distribution) is copied into the mode's root first, unless `--in-place`. With `integrate = "none"` in
`[integration.portable]`, a portable install touches neither the registry nor the Start Menu, and `gupkg shell` prints the
line that puts `<root>\bin` on `PATH`.

### The machine interface

The documents and options below are a public, versioned contract (`"api": 1`); `gupkg-manager` and scripts rely on them.

| Option | Meaning |
| --- | --- |
| `--json` | One result document on standard output (`api`, `command`, `ok`, `exit_code`, then `results` with one entry per target: `package`, `operation`, `ok`, `status`, `version`, `previous`, `mode`, `exit_code`, `receipt`, `log`, `warnings`, `details` for what the operation did, and, on failure, `error` with `code`, `message`, `hint` (only when there is one) and what it carries (`invalid-checksum` carries the `version` and the `field`), such as the `url` of `checksum-missing`). Human output goes to standard error. |
| `--plan` | `check-updates`, `install`, `update`, `uninstall`, `switch`, `cleanup`, `link` and `self ...` resolve and plan without changing anything (except the run's log files). The plan document has one entry per target with the chosen version, the mode, `requires_elevation`, `sha256` (`present` or `missing`; a malformed declared hash makes the version invalid, so that target has no plan: it is a failed entry with the stable error code `invalid-checksum` (plus `version` and `field`), exit code 2, which `--skip-checksum` never overrides), the source `url` and the `effects` to add or remove. |
| `--events-file NAME` | The run's event stream as JSON lines, flushed per event, under the plain file name `NAME` in the log directories: `<root>\.gupkg\logs\NAME` (global events) and `<root>\<name>\.gupkg\logs\NAME` (one package's events). `run_start` names the run's stop marker; the last line, `run_end`, carries the result document. An elevated child chooses the directory itself, so a normal-shell caller reads the file there. `NAME` with a separator, `..`, a drive or a device name is a usage error. |
| `--no-input` | Never asks. A question that would be asked (`uninstall --purge`, a call with several targets, the elevation of a relaunch) is exit code 5 with a structured error and nothing changed. `--yes` answers those questions. The command line never asks about checksums. |
| `--skip-checksum[=REASON]` | Installs a download that has no declared SHA-256, over `https://` only. Bare, the receipt records `skipped-cli`; the manager relays `=setting` or `=user-approved`; any other value is a usage error. |

A stop request is a file: `run_start.stop_file` is the path of a marker that is unique to the run; creating a file there
makes the run finish the package it is working on and skip the rest (exit code 5). An elevated run honours it too, since it
can only make the run stop.

Exit status: `0` success, `2` user or configuration error (including usage errors), `3` the operation failed,
`4` internal error, `5` declined or input needed, `6` the package needs a newer gupkg (an unknown `pkg.toml` `format`, or an
unmet `requires_gupkg`: a package launcher then runs its bundled gupkg).

0.17 spellings are not aliases: `config-check`, `config-fix`, `doctor`, `repair-deps` and `update-deps` are unknown
commands, and `--scope`, `update --check-only`, `update --all` and `update --check` are usage errors that name what to use now (every
other 0.17 option is simply an unrecognized argument). `gupkg tui`, `gupkg manager` and `gupkg migrate` are permanent forwarders to
`gupkg-tui`, `gupkg-manager` and `gupkg-migrate` of the same gupkg; they pass the exit code through. `tui` and `manager` refuse an
elevated shell (they ask for administrator rights themselves, once per batch); `migrate` does its own work and accepts one.

gupkg runs only on its bundled Python: `python -m gupkg` or `bootstrap.py` on any other interpreter, or without `-I`, exits
with `Start gupkg through gupkg.cmd or gupkg.exe; it runs only on its bundled Python.` In a source checkout use
`scripts\run.cmd`. `src\gupkg\gupkg.cmd` runs only `python\python.exe` from its own folder, as
`python.exe -I -s -B -S bootstrap.py`; when it is missing it exits with status 1 and tells you to reinstall gupkg from a
release ZIP.

### Settings, registries and authoring

`gupkg settings show` lists every effective setting with the layer it came from; `settings path` names the file of each
layer; `settings init` and `settings set KEY VALUE` write the user layer (`--mode system` writes the system layer from an
administrator shell) after the schema accepted the value. `gupkg registry add NAME SOURCE` and `remove NAME` edit
`[[registry]]` the same way; `registry sync` downloads the registries into `<root>\.gupkg\registry\<name>`.
Converting a 0.17 manager configuration is `gupkg migrate settings`.

`gupkg pkg new NAME[@VERSION] [--track github:OWNER/REPO]` writes a starter; `pkg check PATH [--install]` validates a
version directory, a tracker or a whole package folder (`--install` also installs into a throw-away root on a pretend
machine; the registry's CI can run `gupkg pkg check <path> --json` in place of calling `gupkg.pkgcheck.check_directory`);
`pkg pin TARGET` copies the hash that a receipt observed for an install made with `--skip-checksum` into `pkg.toml`, after
it shows the URL, the final URL, the size and the date and says that the hash comes from an unverified download;
`pkg export VERSION-DIR -o X.zip` writes a distribution ZIP and prints its SHA-256; `pkg bundle VERSION-DIR` copies the
running gupkg's verified bundle into a version directory.

### Output and logs

A run reports what it does as events: phases (`Resolve`, `Fetch`, `Verify`, `Unpack`, `Prepare`, `Activate`, `Integrate`,
`Finish`), steps, download progress, the lines that `git` and package scripts print, warnings and errors. On a terminal they
appear as coloured phase rules with a check or cross and the elapsed time, a live download bar and a closing summary, which
is always the last thing printed. When output is redirected, or `NO_COLOR` is set, the same events print as plain text:
`==> Phase` headers, no colour codes, no carriage returns and only the final progress line of each download. Every warning
and error is printed once.

Every run that acts on a package leaves, in that package's `<root>\<name>\.gupkg\logs\`, a
`<UTC yyyymmddThhmmssZ>-<command>.log` (plain text, flushed per line) and a `.jsonl` file with the same events. The run as a
whole leaves a log in `<root>\.gupkg\logs\` that holds only global information and the paths of the package logs it links to.
The closing summary names every log, each receipt stores the log of the run that wrote it, and `gupkg logs <target>` lists,
prints (`--path`) or opens (`--open`) them. A run that involves no package, started by a bundled gupkg, logs in its own
package's `.gupkg\logs\`; one started by an installed gupkg logs in its root's `.gupkg\logs\`. `latest.log` in each log
folder is a copy of the newest text log. `[logs] keep_runs` and `keep_days` of the settings (default 50 runs, 90 days) are
applied when a run starts. gupkg never logs to AppData or ProgramData, and a failure to write a log produces one warning
and never fails the operation. `list`, `info`, `search`, `logs`, `shell`, `settings` and `pkg` read or edit and leave no
log; the lifecycle commands, `registry sync` and `self` do.

Details that scripts and maintainers may rely on:

- **Streams.** Without `--json`, warnings and errors go to standard error and everything else (phases, steps, progress, the
  summary) to standard output. With `--json`, all human output goes to standard error.
- **Text from packages is cleaned for display.** Escape sequences and control characters in step, warning, error and
  child-process text are removed on the console and in the text log. The JSONL logs keep step, warning and error text verbatim
  (JSON escapes it); child-process lines are cleaned where they are captured.
- **Event contract.** Each `.jsonl` line is `{"event": <name>, <fields>, "ts": <UTC>}` (`"api": 1` in `run_start`), plus the
  `log_link` record in run logs. `subprocess_line.stream` is always `"output"` in this release.
- **Events file names.** `--events-file` accepts ASCII letters, digits and `._+-` only. A name that ends in `.log`, starts with
  `latest.`, or looks like a run's own file is refused, and an existing hard link, link or directory under the name is never
  opened (a stale plain file is replaced).

## `pkg.toml` reference

`pkg.toml` is optional for a simple pre-populated package. If it is absent,
`install` uses defaults and does not create a file. Use `config-fix` to create a
starter configuration, synchronize directory-owned metadata, or explicitly
convert legacy metadata. Existing files are backed up with a UTC timestamp
before a change unless `--no-backup` or `--backup=false` is supplied. When a
file exists, its schema is strict: unknown keys and legacy spellings are errors.

The package name, version, and local revision come only from the directory
names (`Ripgrep\14.1.0.local.1`); `pkg.toml` does not record them, and any copy in
an old file is ignored. `only_portable` must agree with the `-portable`
naming convention. `config-fix` removes stale name, version, and localVersion
lines and synchronizes `only_portable`, preserving unrelated configuration and
comments where possible. An install stops on a portable-flag mismatch.

```toml
only_portable = false            # must agree with the -portable convention
description = "Fast text search" # descriptive metadata only
homepage = "https://example.com" # descriptive metadata only

[origin]
url = "https://example.invalid/ripgrep.zip"
checksum = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
extractSubdir = "ripgrep-14.1.0"

[[shortcut]]
name = "Ripgrep"
targetPath = "$App\\rg.exe"

[[environment]]
Name = "RIPGREP_HOME"
Value = "$App"

[[path]]
value = "$App"

[[bin]]
name = "rg"
target = "$App\\rg.exe"
arguments = ["--color=auto"]
```

Top-level keys are exactly: `description`, `homepage`, `only_portable`, `origin`, `update`, `shortcut`, `environment`,
`path`, and `bin`.

### Application origins

`[origin]` is optional only when the version already contains a non-empty
`App`. It supplies `App` when that directory is missing or empty,
unless `--refresh-app` is used. An origin is one of the following.

**ZIP origin.** Omit `mode` and supply an HTTP(S) archive URL. `checksum`, when
present, must be `sha256:` followed by 64 hexadecimal characters. `extractSubdir`
selects a directory inside the archive; its contents become `App`.

```toml
[origin]
url = "https://example.invalid/tool-portable.zip"
checksum = "sha256:<64-hex-digits>"
extractSubdir = "tool-portable"
```

**Git origin.** Set `mode = "git"`, provide a safe Git URL, and optionally a
full `refs/...` ref. The ref defaults to `refs/heads/main`. `gupkg` resolves the
ref and checks out that exact commit into `App`.

```toml
[origin]
mode = "git"
url = "git@github.com:owner/repository.git"
ref = "refs/heads/main"
```

**Script origin.** Supply a package-local `.ps1`, `.cmd`, `.bat`, or `.exe`
path. It must stay beneath the version directory. `gupkg` runs it with its
directory as the working directory, sends a JSON document on standard input,
and requires a successful exit status and a non-empty `App`. The JSON contains
`config`, `identity`, and `PkgVars` (`PkgRoot`, `App`, `Icons`, and
`Shortcuts`). Script origins clear `App` first only when `--refresh-app` is
used.

```toml
[origin]
script = "scripts\\populate-app.ps1"
```

**Module origin.** Supply a package-local Python module beneath `pkg.local`.
It must declare `PKG_MODULE_API = 1` and define `populate_app(context)`. The
module receives the same `config`, `identity`, and `PkgVars` mappings as a
script origin, plus `apiVersion = 1`.

```toml
[origin]
module = "pkg.local/populate_app.py"
```

`url`, `script`, and `module` are mutually exclusive. Set `mode = "module"`
only when declaring `module`; ZIP, script, and module modes are otherwise
inferred. Git origins cannot use `checksum` or
`extractSubdir`.

Use repeated `[[origin.versions]]` tables to record historical sources. Every
entry needs a unique `version`; it may initially contain only that version.
If `[origin]` has no inline `url` or `script`, the entry matching the package's
top-level `version` becomes the active source.

```toml
[origin]

[[origin.versions]]
version = "1.0.0"
url = "https://example.invalid/tool-1.0.0.zip"
checksum = "sha256:<64-hex-digits>"
```

### Integration tables

Each `[[shortcut]]` table accepts `name`, `targetPath`, `arguments`,
`workingDirectory`, `iconLocation`, and `description`. Only `name` and
`targetPath` are required. A `.lnk` suffix is added to `name` when absent.

Each `[[environment]]` table requires exact-case `Name` and `Value` keys.
Values are written as expandable Windows registry strings. Each `[[path]]`
table has one `value`; normalized entries are appended only when they are not
already present (case-insensitive, ignoring a trailing slash).

Each `[[bin]]` table normally declares a native executable shim with `name`
and `target`:

```toml
[[bin]]
name = "tool"
target = "$App\\tool.exe"
type = "console"            # default; use "gui" for desktop applications
arguments = ["--safe"]      # fixed arguments precede caller arguments
forward_args = true          # default
elevate = false              # default
working_dir = "$App"        # optional
```

The installed command is `<name>.exe` (an explicit `.exe` suffix is also
accepted) with a matching `<name>.config.toml`. The launcher invokes `target`
directly, without a shell. Use `type = "gui"` when the command should not
create or attach to a console.

When shell behavior or custom file content is necessary, `content` remains an
explicit escape hatch. It cannot be combined with shim options:

```toml
[[bin]]
name = "tool.cmd"
content = "@echo off\r\ncall \"$App\\tool.cmd\" %*\r\n"
```

Shortcut and wrapper names are expanded before placement. A simple name goes
under the scope's default root; nested relative paths are allowed. Absolute
paths and `..` traversal are also allowed, but an output outside the default
root produces a warning. This is intentional flexibility, so review such
configuration carefully.

### Variables and expansion

`$App`, `$VersionRoot`, `$Icons`, and `$Shortcuts` expand to the corresponding
directories in the selected version. `$VersionRoot` is the
version directory itself. `${version}` expands to the upstream version. Braced
environment references such as `${USERPROFILE}` expand from the process
environment and must resolve. `$$` becomes a literal dollar sign.

In normal configuration fields, an unbraced non-package token such as `$NAME`
is an error. In `[[bin]].content`, such tokens remain literal so native batch,
PowerShell, and shell variables work as expected.

## Updates

Updates follow **check → download → install**. `gupkg update --check-only` only
discovers a candidate. `gupkg update --download-only` stages a complete new version under
`<package-root>\.gupkg\work`, commits it as a new `<version>` directory,
and records a receipt. `gupkg update` activates the most recently
downloaded version through the regular install workflow.
`gupkg update` performs those three steps as one explicit command.
Update state, locks, receipts, and disposable work files all live in
`<package-root>\.gupkg`; package repositories should ignore `/.gupkg/`.

Updates are never started or activated automatically. A package administrator
or a scheduler chooses when to run each explicit update command.

Every update needs `[update.payload]` and normally `[update.check]`:

```toml
[update.check]
mode = "github"
assetName = "tool-${version}-windows-x86_64.zip"
# tagPrefix = "release/" # remove a publisher-specific namespace before parsing

[update.payload]
mode = "zip"
extractSubdir = "tool"
```

#### Update checks

`[update.check].mode` is one of:

- `github`: requires `[origin].url` to be an `https://github.com/owner/repository`
  page URL and requires `assetName`. The built-in checker reads GitHub's latest
  release, removes a conventional leading `v` from its tag, and requires
  exactly one uploaded asset with that name. `${version}` in `assetName`
  expands to the discovered release version. GitHub's SHA-256 asset digest is
  used when available. Set `tagPrefix` when the publisher namespaces tags (for
  example, `release/v1.2.3` with `tagPrefix = "release/"`).
- `git`: checks `appPath` (default `App`) against `remote` (default `origin`)
  and a full `ref` (default `refs/heads/main`). For a Git origin, `check` may
  be omitted and defaults to that origin's ref; an explicit ref must match it.
- `module`: imports a trusted `.py` module beneath `pkg.local` (default
  `pkg.local/check_update.py`) and calls `check_update(context)`. `channel`
  defaults to `stable`.

Module checks must declare `PKG_MODULE_API = 1`. Their context has
`apiVersion`, `current` identity fields (including `appReady`),
package/version/App paths, persisted state, and `channel`. Return `None` only
when the upstream version is current and its payload is healthy. When
`appReady` is false, return the current candidate again so `update --download-only`
can stage a repair. Candidate mappings contain non-empty `candidateId`,
`version`, and `url`. Optional candidate fields are
`sha256` (64 hex digits), `fileName`, `headers`, and `extractSubdir`. Candidate
versions must be safe version-directory values and may not go backward.

`gupkg` never installs dependencies at run time, neither its own nor a hook's.
Hooks run once and may import only the standard library, the modules the gupkg
bundle ships, and modules in their own `pkg.local` folder. When a hook needs an
unavailable import, the update fails with a message naming the module and
nothing is installed or changed. Trusted package-local hooks are not sandboxed.

#### Update payloads

`[update.payload].mode` is one of:

- `zip`: downloads a verified ZIP payload and populates a new `App`. A direct
  candidate `.exe` is instead copied intact into `App`; installers need a
  module payload.
- `module`: downloads the candidate artifact and calls trusted
  `pkg.local/unpack_app.py` by default. It must declare `PKG_MODULE_API = 1`
  and define `unpack_app(context)`. The context contains `candidate` and paths
  for `artifact`, `stageRoot`, and `stageApp`; the hook must leave `stageApp`
  non-empty.
- `git`: clones the checked Git candidate into a new immutable version.

`git` requires a Git check. Downloaded candidates require a SHA-256 checksum
unless `ignore_checksum = true` in the payload or
`--no-checksum` is used.

ZIP payloads may use `extractSubdir`, a candidate-provided `extractSubdir`, or
explicit extraction mappings. `[[update.payload.extract]]` uses a ZIP-root
shell-wildcard `src` and an `$App`-relative `dest`. A `src` ending in `/` copies
the matched directory's contents; otherwise a matched directory is copied as a
directory. `extract` cannot be combined with `extractSubdir`.

```toml
[update.payload]
mode = "zip"

[[update.payload.extract]]
src = "pandoc-*/"
dest = ""

[[update.payload.rename]]
src = "pandoc-${version}.exe"
dest = "pandoc.exe"
```

`[[update.payload.rename]]` renames exact, safe paths inside staged `App`
after extraction; it cannot overwrite an existing destination. `maxSizeMB` is
accepted by the current schema for future policy use but is not currently
enforced.

#### Install steps

The built-in update sequence is a release check followed by the configured
payload step. Existing packages need no step declaration. To run package-local
work after that payload has been unpacked, declare the ordered sequence
explicitly. The first entry must be the built-in `payload` step; each later
entry is a trusted Python module beneath `pkg.local`.

```toml
[[update.steps]]
mode = "payload"

[[update.steps]]
mode = "module"
module = "pkg.local/post_install.py"
```

Install-step modules declare `PKG_MODULE_API = 1` and define
`install_step(context)`. They receive the candidate and `paths` for the staged
`stageRoot`, `stageApp`, and downloaded `artifact` (or `None` for Git
payloads). Steps run in declaration order before the new version is committed,
so a failure leaves the installed version unchanged.

```python
PKG_MODULE_API = 1

def install_step(context):
    context["paths"]["stageApp"].joinpath("unwanted-file.txt").unlink(
        missing_ok=True
    )
```

Versions beginning with `bootstrap` are templates rather than active payloads.
Installing one with a Git or module update configuration downloads and
activates the first immutable version, leaving the template itself without an `App`.
A Git bootstrap is a `_bootstrap` template with `payload.mode = "git"`.

## Configuration, validation, and migration

Use `config-check` before deploying a package definition. It validates canonical
TOML, directory-derived metadata, historical-origin consistency, package-local
origin scripts, and configured update modules without modifying the package.

Use `config-fix` to create a starter `pkg.toml` or repair metadata while
keeping runtime settings. It imports `_shortcuts` `.lnk` files by default and
archives each imported source as `.lnk.imported`; pass
`--import-shortcuts false` to skip that import. It does not populate `App` or
install components.

`config-fix` is the explicit migration path for older JSON-based layouts. It
uses the existing converter, validates the complete replacement before writing,
and creates a timestamped sibling backup unless backup is disabled. Use
`--output PATH` only for this conversion mode.

The standalone helpers remain available from `src`:

```bat
python gupkg\legacy_to_gupkg_toml.py --dir C:\Packages\Ripgrep\14.1.0.local.1
python gupkg\shortcuts_to_gupkg_toml.py --dir C:\Packages\Ripgrep\14.1.0.local.1
```

The shortcut importer reads `.lnk` files from `_shortcuts`, converts
package-owned paths back to package variables, and updates matching
`[[shortcut]]` tables. After a successful write, it archives each imported
source as `.lnk.imported`. Both helpers are migration tools, not the supported
runtime package API.

## Development

Implementation and test guidance is in [_docs/development_guide.md](_docs/development_guide.md).
The runtime module overview and migration-helper details are in
[src/gupkg/README.md](src/gupkg/README.md).
