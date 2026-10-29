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
gupkg-manager update --all --yes       :: the same for every package you manage (the separate manager program)
```

**Why use it**

- **Minimal friction.** One folder per program, one readable file per folder.
  No installers to click through, no registry archaeology.
- **Safe by construction.** A declared SHA-256 is always verified, a download with none is
  refused unless you say `--skip-checksum`, and every version is built in a separate
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
| Run gupkg for many machines: settings, registries, schedules, recovery | [Operations guide](_docs/operations.md) |
| Look up every `pkg.toml` field | [`pkg.toml` reference](#pkgtoml-reference-format-2) below |
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
  gupkg-bootstrap.cmd -Scope user      :: just you (refused from an elevated shell: gupkg never acts on your folders as administrator)
  gupkg-bootstrap.cmd -Scope system    :: all users (needs an elevated shell)
  ```

  The script is thin: it downloads the newest release ZIP
  (`gupkg-<version>-win-amd64.zip`, which `RELEASES.md` lists with its SHA-256),
  checks the download against that digest, unpacks it and runs the
  `install.cmd` of the version directory it holds. That launcher starts the gupkg
  bundled in the ZIP, which installs the `gupkg` package like any other: it copies
  the bundled tree into `App` (checking every file against `MANIFEST.sha256`; a
  modified, missing or extra file stops the install), creates `current`, the
  `gupkg`, `gupkg-tui` and `gupkg-manager` commands, their `PATH` entry and the
  Start Menu entry, and writes the root data folder `<root>\.gupkg\` with the
  one-line file `gupkg-location` through which the package launchers of that
  root find the installed gupkg. In user scope the ZIP is unpacked to a staging folder and the
  result is `%USERPROFILE%\opt\gupkg\<version>`. In system scope the script
  creates `C:\opt` owned by Administrators and writable by nobody else (it refuses
  an existing `C:\opt` that is not like that), unpacks straight into it and runs
  `C:\opt\gupkg\<version>\install.cmd` elevated, which first checks that
  everything it will run is admin-only. It is safe to repeat. The only downloads are `RELEASES.md` and
  the ZIP. Use `-Source` with a ZIP (URL or path) and `-Sha256` to install a vetted
  release from your own share, or the distribution of another program in user
  scope (that installs only that program, never gupkg); `-SkipInstall` only
  verifies and unpacks. Nothing needs to be installed first: the ZIP carries its
  own Python, the locked dependencies and the native launchers.

  A ZIP downloaded with a browser (and extracted with Explorer) has the Mark of
  the Web on every file, so Windows asks before it runs the first `install.cmd`
  ("Open File - Security Warning"): click Run, or tick Unblock in the ZIP's
  Properties before extracting it. Files that gupkg copies into a root carry no
  mark, so later launchers do not ask. See the
  [troubleshooting guide](_docs/troubleshooting.md#a-security-warning-when-running-installcmd-or-smartscreen-and-smart-app-control).
- **Release ZIP by hand.** The ZIP built by `tools\build_standalone.py` (and
  attached to each GitHub release) is the only distribution. Give it to the
  bootstrap script as `-Source`, or unzip it and run
  `gupkg\<version>\install.cmd`: with no installed gupkg in reach, the launcher
  runs the gupkg bundled beside it. Every distribution made with
  `gupkg pkg export` installs the same way, and installs only its program.
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

`gupkg` manages self-contained Windows applications that live on disk rather than in a central package store. A package
author puts an application's files, its version number (the folder name) and a small `pkg.toml` in one directory. From that
definition, `gupkg` makes the application available to Windows: Start Menu shortcuts, environment variables, `PATH`
entries and command-line shims. It can fetch the application's files when they are not present, and it finds, downloads
and prepares new versions when you ask it to.

The model is repair-friendly: running `install` again re-applies the declared Windows integration without downloading an
application whose files are already there, and `check` finds what drifted.

## At a glance

- Windows only. The release ZIP carries its own Python; nothing else needs to be installed.
- **One scope per run.** A run works in one *scope*: a mode and one root. The mode is that of the process: an elevated
  shell is `system` (all users, `C:\opt`), any other shell is `user` (`%USERPROFILE%\opt`). There is no mode option and no
  setting that selects one, and gupkg never starts an elevated copy of itself: to manage system packages, start gupkg or
  `gupkg-manager` from an administrator shell (or the Start Menu entry "gupkg manager (administrator)"). `--root DIR`
  chooses another root of the same mode. A package says which modes it allows (`modes`); a **portable** package
  (`modes = ["portable"]`, state kept in the package folder) is its own package and installs for a single user only.
- **Format 2 `pkg.toml`.** One `[source]` per version, a separate `[track]` table in a tracker folder, built-in services
  instead of scripts (version discovery, unpacking, checksum discovery, post-unpack steps), and `snake_case` everywhere.
- **Checking and installing are separate.** `check-updates` runs a package's tracker and writes (and by default prepares) a
  new pinned version; `update` activates it. Nothing is ever activated by a check.
- **Checksums.** A declared SHA-256 is always verified, and a mismatch is always fatal. A download with no declared hash is
  refused unless the call carries `--skip-checksum` (https only, never for vendor installers).
- **One command line, three programs.** `gupkg` (no screens, no `--all`), `gupkg-tui` (one package) and `gupkg-manager`
  (every package, batches, schedules); the last two only ever call `gupkg`.
- Everything gupkg owns lives in the root: settings, logs, state, the registry cache and `gupkg-location` are in
  `<root>\.gupkg\`; nothing is written to `AppData` or `ProgramData`, and the Windows registry is only ever written to.

## On-disk layout

A root (the run's root; each mode has its own) holds package folders. A package folder holds immutable **version directories** and a
`current` junction that points to the active one:

```text
<root>\                                # C:\opt or %USERPROFILE%\opt
  .gupkg\                              # root data: settings.toml, state.toml, logs\, registry\, gupkg-location
  <name>\                              # the package folder: its name is the package's identity
    current\                           # NTFS junction to the active version
    _check_updates\                    # optional tracker (never installed): pkg.toml with [track], pkg.local\, support files
    1.2.3\                             # a pinned version directory
      pkg.toml
      App\                             # the payload: the files that run (empty or absent until populated)
      pkg.local\                       # optional trusted hook modules and populate scripts
      install.cmd, gupkg-tui.cmd       # package launchers (double-click installs / opens this version)
      gupkg\                           # optional: a bundled gupkg (bundled mode)
    1.2.4\                             # pinned, not populated yet: status "available"
    data\                              # portable packages only: persistent state
    .gupkg\                            # package.toml (install record), logs\, locks\, work\, state\   (each <version>\App.info records how its App was built)
```

A **version directory** is any child of the package folder that is not reserved and contains a `pkg.toml`; the folder name
is the version (any valid folder name: `1.2.3`, `2024-05`, `nightly`; text after the first `.local.` is a local revision).
Versions order by dotted segments, numbers numerically and text alphabetically, and a version without a local revision
sorts before the same version with one. Reserved names are `current`, `_check_updates`, `data` and anything starting with
`.` or `_` (0.17's `_bootstrap` is just such a reserved name now). The package name is the folder name; a version's number
is never repeated inside `pkg.toml`. Population is atomic: `App` is built in `.gupkg\work` and renamed into place, and
`current` is switched only after a successful population. Old versions stay until `gupkg cleanup` is run.

`App` is the payload, and gupkg never invents a layout in it. Shortcuts, `PATH` entries, environment values and shims
normally point at files beneath `App`. How `App` is filled is the version's `[source]` (see below): **downloaded**
(recoverable at any time), **script-populated**, or **shipped** (already in the folder; gupkg never drops or replaces it,
and `uninstall --purge` is the only thing that deletes it). Every install records the SHA-256 of every file of `App` in the
receipt, and `gupkg check` reports modified, missing and extra files.

You may give `gupkg` a package name, a version directory, a package folder or its `current` junction. A package folder
without `current` is accepted when it holds exactly one version.

**Package launchers.** Each version directory holds `install.cmd` and `gupkg-tui.cmd` (a tracker folder holds
`check-updates.cmd`), so that a double-click works without gupkg on `PATH`. A launcher runs the first gupkg it finds, in this
order: the gupkg bundled in its own version directory (`gupkg\`); the folder named by the one-line file
`<root>\.gupkg\gupkg-location` (written when the `gupkg` package is installed into the root, removed when it is uninstalled,
and ignored when it is missing, empty or names a folder without `gupkg.cmd`); `gupkg.cmd` or `gupkg.exe` on the `PATH` of the
process. Run elevated, a launcher first checks that everything it will run (the version directory, the package folder, the root,
`.gupkg\gupkg-location`, and the whole gupkg tree it selected) is writable by Administrators and SYSTEM only, takes only
`PATH` entries whose folder passes that check (it never reads the registry), and otherwise refuses. A gupkg that cannot handle the
package (exit code 6) makes the launcher try the next one.

## Commands and options

One grammar serves people, scripts and the UI programs. Options go **after** the command; the only options
before it are `--version`, `--help`, `--settings PATH`, `--root DIR` and `--sandbox DIR`. A target is a package name
(`firefox`, `firefox@135.0`, `main/firefox`) or a path (anything with a `\` or `/`, or starting with
`.`). A target never names a mode (a `user:` or `system:` prefix is refused): the mode is that of the process. Several targets in one call run one after another and the run continues past a failure. The command line has
no `--all` and no interactive screen: `gupkg-manager` is the program for batches and interactive use.

```text
gupkg check-updates <target>... [--no-download] [--skip-checksum]   run trackers; write and prepare new versions; activate nothing
gupkg install  <target>... [--skip-checksum] [--refresh] [--yes] [--allow-downgrade] [--allow-other-broken-versions]
gupkg update   <target>... [--skip-checksum] [--yes] [--allow-downgrade] [--allow-other-broken-versions]   activate the newest KNOWN version; never runs a tracker
gupkg switch   <target> <version>                       activate another local version (rollback)
gupkg uninstall <target>... [--purge] [--yes]           remove the integration; --purge also deletes the package folder and its data
gupkg check    [<target>...] [--fix]                    health: drift, App fingerprint, registry fingerprint, unverified installs
gupkg list     [--installed|--available|--outdated|--broken|--local] [--details] [--names]
gupkg info     <target>...
gupkg search   <query> [--registry R] [--offline]
gupkg cleanup  [<target>...] [--keep N] [--bundled]     old Apps, receipts, work folders, registry trees, logs (only when asked)
gupkg link     <target> [<registry>/<name>] [--unlink]  follow another registry entry, the same-name entry again, or none
gupkg logs     [<target>] [--open | --path]
gupkg shell                                             the PATH line of a root with integrate = "none"
gupkg registry sync|status|list|add|remove
gupkg settings show|path|init|set KEY VALUE
gupkg pkg new|check|pin|bundle|export
gupkg self status|repair|check-updates|update
gupkg tui <target> | gupkg manager ...     start gupkg-tui, gupkg-manager of this gupkg
```

Checking and installing are separate operations. `check-updates` runs a package's tracker and writes the new
version as a pinned directory (`<root>\<name>\<version>\`), by default with its `App` downloaded, verified and
unpacked; it never activates anything. `update` then activates the newest version that is already known, locally or in
the followed registry, and prepares its `App` first if it has none. `install` and `update` never run a tracker, so
`update` of a package with nothing newer says "up to date" and makes no request.

Every command takes `--json`. Every mutating command takes `--yes`. The mode comes from the process and from nothing
else: an elevated process is in `system` mode and works on the system root, any other process is in `user` mode and works on
the user's root. gupkg never starts an elevated copy of itself and has no mode option, no setting that selects a mode and no mode
prefix on a target. A call that needs the other mode is refused with the exact command to run (a system-only package
from a normal shell: use the Start Menu entry "gupkg manager (administrator)" or an administrator shell). Scheduled callers pass
a unique `--events-file NAME` and treat a missing `run_end` line as a failure; a call with several targets needs `--yes` under
`--no-input` (the question "run this on N packages?" is exit code 5 otherwise). Redirected output is UTF-8 and the `--json`
document is pure ASCII, whatever the code page.
### Scopes and install modes

A run's scope is the mode of its process plus one root. `--root DIR` (before the command) works on another root of the same
mode; in system mode the folder must be admin-only. The root otherwise comes from a fixed chain, first match wins: in system
mode the root of the installed system gupkg, `[roots].system` of `$App\settings.toml`, `C:\opt`; in user mode `[roots].user` of
`--settings PATH`, the root the running gupkg is installed in (when that root is not admin-only), `[roots].user` of
`$App\settings.toml`, `%USERPROFILE%\opt`. A root's own `settings.toml` never names a root. The Resolve phase prints the root and
where it came from. A run reads and writes only its own root: nothing under the other mode's root is read, and a path into it
is refused as "not inside this scope's root".

| Mode | Root (default) | Start Menu | Environment and `PATH` | Shims |
| --- | --- | --- | --- | --- |
| `user` | `%USERPROFILE%\opt` | `%APPDATA%\Microsoft\Windows\Start Menu\opt` | `HKCU\Environment` | `%USERPROFILE%\bin` |
| `system` | `C:\opt` (admin-only) | `%ProgramData%\Microsoft\Windows\Start Menu\opt` | `HKLM` environment | `C:\bin` |

`gupkg install firefox` and `gupkg install %USERPROFILE%\opt\firefox` put the same shortcuts, shims and `PATH` entries in place. A
version directory outside the root (an unzipped distribution) is copied into the root first, and the install runs from the copy;
nothing is installed in place, and no other folder becomes a root on the fly. To keep a folder where it is, make it the root of its own user scope:
`gupkg install <folder> --root <folder above the package name>`. Any other path outside the root is refused. With
`integrate = "none"` in the root's `[integration]` table, an install touches neither the registry nor the Start Menu, its shims go
to `<root>\bin`, and `gupkg shell` prints the line that puts `<root>\bin` on `PATH`; that is how a portable root on a stick works
(`--root E:\opt`).

A package says which modes it allows with `modes`. A package that does not allow the run's mode is refused before any work, and
the message says "This package installs only for a single user. Run gupkg without administrator rights." or "This package installs only system-wide. Run gupkg as administrator: use the Start Menu entry "gupkg manager (administrator)", or run in an administrator shell: gupkg <command> <name>" and names nothing of the other mode. `uninstall`, `check` and `cleanup` are never refused this way. `list --available`
also shows packages the mode cannot install, marked `system only` or `user only`. A package folder whose install record names the
other mode is `broken` ("moved between scopes") and is repaired by reinstalling it.

`install` and `update` ask before two things, and `--yes` answers both: installing a version **older than the active one**
(`--allow-downgrade` answers only that), and installing while **other version directories of the package are broken or not
supported** by the mode or this gupkg (`--allow-other-broken-versions` answers only that). With `--no-input`, or with nobody to ask,
either question is exit code 5 and nothing is changed. `switch` (the explicit way back) and a repair of the active version ask nothing.

### The machine interface

The documents and options below are a public, versioned contract (`"api": 2`); `gupkg-manager` and scripts rely on them. `list`, `list --names`, `info` and `search` documents carry `mode` and `root` once, at the top.

| Option | Meaning |
| --- | --- |
| `--json` | One result document on standard output (`api`, `command`, `ok`, `exit_code`, then `results` with one entry per target: `package`, `operation`, `ok`, `status`, `version`, `previous`, `mode`, `exit_code`, `receipt`, `log`, `warnings`, `details` for what the operation did, and, on failure, `error` with `code`, `message`, `hint` (only when there is one) and what it carries (`invalid-checksum` carries the `version` and the `field`), such as the `url` of `checksum-missing`). Human output goes to standard error. |
| `--plan` | `check-updates`, `install`, `update`, `uninstall`, `switch`, `cleanup`, `link` and `self ...` resolve and plan without changing anything (except the run's log files). The plan document has one entry per target with the chosen version, the mode, `sha256` (`present` or `missing`; a malformed declared hash makes the version invalid, so that target has no plan: it is a failed entry with the stable error code `invalid-checksum` (plus `version` and `field`), exit code 2, which `--skip-checksum` never overrides), the source `url` and the `effects` to add or remove. |
| `--events-file NAME` | The run's event stream as JSON lines, flushed per event, under the plain file name `NAME` in the log directories: `<root>\.gupkg\logs\NAME` (global events) and `<root>\<name>\.gupkg\logs\NAME` (one package's events). `run_start` names the run's stop marker; the last line, `run_end`, carries the result document. `NAME` with a separator, `..`, a drive or a device name is a usage error. |
| `--no-input` | Never asks. A question that would be asked (`uninstall --purge`, a call with several targets) is exit code 5 with a structured error and nothing changed. `--yes` answers those questions. The command line never asks about checksums. |
| `--skip-checksum[=REASON]` | Installs a download that has no declared SHA-256, over `https://` only. Bare, the receipt records `skipped-cli`; the manager relays `=setting` or `=user-approved`; any other value is a usage error. |

A stop request is a file: `run_start.stop_file` is the path of a marker that is unique to the run; creating a file there
makes the run finish the package it is working on and skip the rest (exit code 5).

Exit status: `0` success, `2` user or configuration error (including usage errors), `3` the operation failed,
`4` internal error, `5` declined or input needed, `6` the package needs a newer gupkg (an unknown `pkg.toml` `format`, or an
unmet `requires_gupkg`: a package launcher then runs its bundled gupkg).

0.17 spellings are not aliases: `config-check`, `config-fix`, `doctor`, `repair-deps` and `update-deps` are unknown
commands, and every other 0.17 option is an unrecognized argument. `gupkg tui` and `gupkg manager` are pass-through commands: they
start `gupkg-tui` and `gupkg-manager` of the same gupkg and return their exit code. The programs run in the scope of their own process
(an administrator shell manages the system root), and they take no root option of their own: `gupkg --root DIR tui` (and `--settings` or `--sandbox` before `tui`/`manager`) is refused. Start
`gupkg-manager --settings PATH` (or `gupkg-tui --settings PATH`) directly, with a file that has `[roots].user`, or start them from the gupkg that is installed in the root.

### The UI programs: `gupkg-tui` and `gupkg-manager`

Two separate programs (one Textual application each, sharing the library `gupkg_ui`) sit beside `gupkg` in the same gupkg
tree. They never change anything themselves: every operation is a call to the `gupkg.cmd` of **their own tree** (never one on
`PATH`) through the machine interface above (`--json`, `--plan`, `--events-file`, `--no-input`), whose `"api"` they check.
Each runs in one scope, the mode of its own process: started from a normal shell it manages the user root, started from an
administrator shell (or the Start Menu entry "gupkg manager (administrator)") it manages the system root. There is no key and no
prompt that switches scope, and the programs never ask for administrator rights. The left of the status line shows the mode and the root
(`user · C:\…\opt`).

```text
gupkg-tui PATH|NAME                        the package view of one package (versions, link, data folder, receipt, log)
gupkg-manager [tui]                        the browser over every package
gupkg-manager check-updates --all [--no-download] [--yes] [--json]
gupkg-manager update --all [--yes] [--json]
gupkg-manager check [--fix] [--yes] [--json]
```

Every screen has a key-hint line on top and a status line at the bottom. Browser keys: `Enter` open, `/` filter, `c` check
for updates (`C` all), `u` update (`U` all outdated), `i` install or repair, `x` uninstall, `h` health check, `l` log, `a` registry
packages (those this mode cannot install are marked `system only` or `user only`), `s` synchronise the registry, `r` reload, `?` help. The package view has `c u r s x h l` (`s` switches version, `X` purges, `H` fixes, `g` reloads). The hint line names the main keys only; `?` lists them all. While something
runs, Escape asks "Cancel after the current package?"; yes asks the CLI to stop after the package it is working on and skips the rest.

`update --all` plans with `gupkg update <targets> --plan --json`, shows the plan and asks **once** (`--yes` answers; with no terminal
and no `--yes`, or with `--json`, it asks nothing and exits 5 with nothing changed). `[manager] checksum_missing` decides a package
whose download has no checksum: `ask` (default) asks once per package at a terminal and behaves as `refuse` in a schedule, `refuse`
never runs it, `skip` runs it with `--skip-checksum=setting`; a malformed declared checksum and a plain `http://` URL are never
skippable; in a system-mode run `--settings` is refused, so only the system root's settings count. With `[cleanup] auto = true`
an update ends with `gupkg cleanup` for the root it updated. `check-updates --all` needs no confirmation (it activates nothing). Exit codes follow the CLI: the highest code of a target, where a
refused package counts 3 and a skipped or declined one 5. `--json` prints one document (the CLI's head, one entry per target and the
calls made) and sends the text to standard error. When a program closes, the summary of the last run and the log path are printed
(`[manager] keep_screen`). `--settings PATH` and `--sandbox DIR` (before the command) are relayed to every call.

A scheduled task works on the root of the account it runs as: a task that runs as a normal user manages that user's root; a task with "Run with highest privileges"
manages the system root, and then needs a gupkg installed in an admin-only folder (an elevated run refuses any other gupkg tree, exit 2). If the program is closed, its window
is closed or Ctrl-C is pressed while a call runs, the call is first asked to stop after its current package and then its whole process tree is ended, so
nothing keeps running unseen. A call that does not exit
after it wrote its result is stopped after 15 seconds and its result is still used (with a warning); a cancelled call that does not stop is killed after 30 seconds.

gupkg runs only on its bundled Python: `python -m gupkg` or `bootstrap.py` on any other interpreter, or without `-I`, exits
with `Start gupkg through gupkg.cmd or gupkg.exe; it runs only on its bundled Python.` In a source checkout use
`scripts\run.cmd`. `src\gupkg\gupkg.cmd` runs only `python\python.exe` from its own folder, as
`python.exe -I -s -B -S bootstrap.py`; when it is missing it exits with status 1 and tells you to reinstall gupkg from a
release ZIP.

### Settings, registries and authoring

`gupkg settings show` lists every effective setting with the layer it came from; `settings path` names the file of each
layer; `settings init` and `settings set KEY VALUE` write `<root>\.gupkg\settings.toml` of the run's root (the user's from a
normal shell, the system root's from an administrator shell) after the schema accepted the value. The settings schema is `schema = 4`:
a file with `schema = 3` is refused with a message that says what to change. `gupkg registry add NAME SOURCE` and `remove NAME` edit
`[[registry]]` the same way; `registry sync` downloads the registries into `<root>\.gupkg\registry\<name>`.

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
The closing summary names every log, the receipt (`App.info`) describes only the App, not the runs, and `gupkg logs <target>` lists,
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
- **Event contract.** Each `.jsonl` line is `{"event": <name>, <fields>, "ts": <UTC>}` (`"api": 2` in `run_start`), plus the
  `log_link` record in run logs. `subprocess_line.stream` is always `"output"` in this release.
- **Events file names.** `--events-file` accepts ASCII letters, digits and `._+-` only. A name that ends in `.log`, starts with
  `latest.`, or looks like a run's own file is refused, and an existing hard link, link or directory under the name is never
  opened (a stale plain file is replaced).

## `pkg.toml` reference (format 2)

Every `pkg.toml` starts with `format = 2`. The format is strict: an unknown key, a key from the 0.17 format, or a second
`[source]` is an error that names the key. A version directory and a tracker use the same format; a tracker additionally
has `[track]`. All keys are `snake_case`. `gupkg pkg check PATH` validates a version directory, a tracker or a whole
package folder and reports every problem at once.

The version is the folder name and the package name is the package folder's name; neither appears inside the file.
`pkg.toml` is optional only for nothing: a version directory without one is not a version.

```toml
format = 2
description = "Fast text search"                       # information only
project_url = "https://github.com/BurntSushi/ripgrep"  # information only, never fetched
modes = ["system", "user"]                             # the modes it may be installed in (default: both), or ["portable"]
requires_gupkg = ">=1.0"                               # minimum gupkg; an older one exits with code 6
record_manifest = true                                 # false: no App fingerprint (very large trees only)

[source]
download_url = "https://github.com/BurntSushi/ripgrep/releases/download/14.1.0/ripgrep-14.1.0-x86_64-pc-windows-msvc.zip"
sha256 = "<64 hex digits>"
sha256_from = "github-digest"                          # provenance, informational
strip = 1                                              # drop one wrapper folder

[[shortcut]]
name = "Ripgrep"
target = "$App\\rg.exe"

[[env]]
name = "RIPGREP_HOME"
value = "$App"

[[path]]
value = "$App"

[[shim]]
name = "rg"
target = "$App\\rg.exe"
```

Top-level keys are exactly: `format`, `description`, `project_url`, `modes`, `requires`, `requires_gupkg`,
`record_manifest`, `source`, `track`, `prepare`, `portable`, `shortcut`, `env`, `path` and `shim`. There is no `version`, `localVersion`, `name`, `homepage`,
`only_portable`, `[origin]`, `[update]`, `[[bin]]`, `[[environment]]`, `ignore_checksum`, a root-level launcher table, `[user]` or `[system]` step table,
and no `$Data` variable.

Three URLs, three names. `project_url` is for people and is never fetched. `[source] download_url` is the artifact of
*this* version. `[track] check_url` (and `repo`/`repo_url` for `github`, `gitlab` and `git`) is the page that is read to
discover newer versions, and only `gupkg check-updates` reads it.

### `[source]`: how a version's `App` is filled

A version has exactly one `[source]`, and one of these kinds:

| Kind | `[source]` | Meaning |
| --- | --- | --- |
| Downloaded | `download_url` | The built-in download script downloads, verifies and unpacks the artifact and runs the `[[prepare]]` steps. `App` can be deleted and rebuilt at any time. |
| Script-populated | `script = "pkg.local/populate.py"` | A custom script fills `App`: a Python hook, or a `.ps1`, `.cmd`, `.bat` or executable that gets the context as JSON on standard input. |
| Shipped | `shipped = true` | `App` is already in the version directory. Nothing populates it and gupkg never drops it. With a `download_url` as well, the URL is used only to recover a missing `App`. |

`shipped` defaults to `false` with a `download_url` or `script` and to `true` otherwise. A `download_url` and a `script`
together are invalid. A version whose `App` is missing and cannot be recovered fails with `App is missing and no way to
recover it.`

| Key | Meaning |
| --- | --- |
| `sha256`, `sha256_from` | The artifact's SHA-256 (64 hex digits) and where it came from (`github-digest`, `vendor-sums`, `registry`, `manual`). A mismatch is always fatal; a malformed value makes the version invalid. |
| `kind` | `zip`, `7z`, `tar` (`.tar.xz`, `.tar.gz`, ...), `sfx`, `nsis`, `msi`, `exe`/`file` (copied as is) or `git`. Inferred from the file; a declared value must agree with it. There is no `inno` kind. |
| `strip = N` or `"auto"` | Remove N levels of a single wrapper folder (`auto`: while the root holds exactly one folder). |
| `subdir` | Keep only this subtree of the archive. |
| `require = ["firefox.exe"]` | Files that must exist after unpacking. |
| `verify_manifest = "path"` | A `<sha256>  <relative path>` manifest inside the artifact; every unpacked file must match it. |
| `headers` | Request headers for the download (a table; `Host`, `Content-Length` and similar are refused). |
| `[[source.nested]]` | Unpack an inner archive: `path`, `dest`, `replace`. |
| `[[source.extract]]` | Glob-mapped partial extraction: `src`, `dest`. |
| `[source.installer]` | Run a vendor installer into the stage (`run = ["/VERYSILENT", "/DIR=$Stage"]`). Needs a pinned `sha256`, is never skippable, and is labelled "runs vendor installer"; `[hooks] allow_vendor_installers = false` forbids it. |
| `[source.git]` | For `kind = "git"`: `ref` and the pinned `commit`, which is the integrity anchor. |
| `[source.checksum]` | Trackers only: how to find the hash of a discovered version (`sidecar`, `sums_file` + `entry`, `json` + `path`, `page_match`, `optional`). A pinned version carries none. |

Unpacking is safe by construction: no path escapes `App`, no links, no device or stream names, limits on entries and
sizes, and a failed unpack leaves no `App` and no scratch folder.

### `[[prepare]]`: steps after unpacking

Run in order against the staged `App`; each row has exactly one of these keys, and every path is confined to `App`:

```toml
[[prepare]]
remove = ["uninstall.exe", "$PLUGINSDIR"]                   # globs allowed; a missing path is ignored
[[prepare]]
rename = { from = "setup_*.exe", to = "tool.exe" }          # exactly one match; never overwrites
[[prepare]]
copy = { from = "shim.exe", to = ["node.exe", "npm.exe"] }
[[prepare]]
write = { path = "portable.ini", content = "..." }
[[prepare]]
module = "pkg.local/fix.py"                                 # escape hatch (hook API 2)
```

### Integration tables

- **`[[shortcut]]`**: `name`, `target` (required), `arguments`, `working_dir`, `icon_location`, `description`. A `.lnk`
  suffix is added to `name`. `run_as_admin = true` sets the "Run as administrator" flag of the link, so that Windows shows the
  UAC prompt when the shortcut is opened (gupkg itself contains no elevation code; gupkg's own "gupkg manager (administrator)"
  entry is made this way). `modes = ["system"]` (a non-empty subset of the package's `modes`, never `portable`) creates the
  shortcut only in those modes; without it the shortcut belongs to every mode of the package.
- **`[[env]]`**: `name`, `value` (exact case). Written as expandable registry strings. Reserved system variables (`Path`,
  `SystemRoot`, ...) are refused.
- **`[[path]]`**: one `value`; added to `PATH` only when not already present. An entry that starts with an environment variable (`%LOCALAPPDATA%\Voltain`) is valid only in a package whose `modes`
  does not list `system`: it differs for every user and a user could redirect it, so the machine `PATH` never takes one, and
  `gupkg pkg check` rejects the file. Prefer a `[[shim]]` to a `PATH` entry.
- **`[[shim]]`**: `name` and `target`, with `type = "console"` (default) or `"gui"`, `arguments` (fixed, before the caller's),
  `forward_args = true` (default), `elevate = false` and `working_dir`. The shim is `<name>.exe` with a
  `<name>.config.toml` in the mode's `bin` folder and starts the target directly, without a shell. For shell behaviour
  write a content shim instead, `content = "@echo off\r\n..."` with a name such as `tool.cmd`; `content` cannot be combined
  with the other shim options. Names may not be device names, streams or end in a dot or space.
- **Names belong to one package.** A shim, wrapper, shortcut or environment variable that another installed package of the
  root already provides is refused (`name-conflict`) before anything is changed: uninstall the other package, or use a
  definition with a different name.
- **`modes`** says where the package may be installed: `["system", "user"]` (the default), `["system"]` or `["user"]`, or
  `["portable"]` for a portable package, which installs for a single user only. A list that mixes `portable` with `system` or
  `user` is invalid: publish a separate `<name>-portable` package. A package whose `modes` lack the mode of the run is
  refused before any work.
- **`[portable]`** (required when `modes = ["portable"]`, and allowed only then): `persist` (paths kept in `<package>\data`
  and linked into every version's `App`) and `markers` (files that switch the program to portable mode). There are no
  per-mode step tables: the install-time steps of every package are its `[[prepare]]` rows
  (`remove`, `rename`, `copy`, `write`, `module`).

### Variables

| Variable | Meaning |
| --- | --- |
| `$App` | The active version's `App`. |
| `$VersionRoot` | The version directory. |
| `$PackageRoot` | The package folder. |
| `$Bin` | The mode's shim folder. |
| `$Stage` | The staging folder (`[source.installer]` only). |
| `${version}` | The version, with filters: `${version\|replace:.:-}`, `${version\|major}`. |
| `${name}` | A named capture from `[track]`. |
| `${ENV}` | A braced environment variable; it must resolve. |
| `$$` | A literal dollar sign. |

In `content` shims an unbraced token such as `%NAME%` stays literal; elsewhere an unknown `$NAME` is an error.

## Trackers and checking for updates

A package that can discover newer versions has a `_check_updates` folder holding a tracker: a `pkg.toml` that is a template
for new versions plus a `[track]` table. A tracker is never installed. `gupkg check-updates NAME` runs it:

```toml
format = 2
description = "Pandoc, a universal document converter"
project_url = "https://pandoc.org"

[track]
provider = "github"
repo = "jgm/pandoc"
asset_pattern = 'pandoc-(?P<version>[\d.]+)-windows-x86_64\.zip'

[source]
download_url = "${asset_url}"
strip = "auto"
# sha256 comes from GitHub's asset digest when GitHub publishes one

[[path]]
value = "$App"

[[shim]]
name = "pandoc"
target = "$App\\pandoc.exe"
```

1. **Check.** The `[track]` provider returns a version and captures (for example the asset URL and its digest).
2. **Compare.** gupkg compares the version with `current` and the local versions; a hook never decides whether
   something is current.
3. **Write the version directory** `<package>\<version>\`: the support files are copied (never update-check code), `pkg.toml`
   is rendered with every template expanded and `[track]` removed, `sha256` is filled only from a hash the tracker
   *discovered* (`sha256_from` says how), and the version's own `install.cmd` and `gupkg-tui.cmd` launchers are written.
4. **Populate it** (unless `--no-download`): download, verify, unpack and prepare, with the same checksum rules as `install`.
   When the rules refuse the download (no hash and no `--skip-checksum`) the directory stays without `App` and the refusal is
   reported.
5. **Report and stop.** The package shows as `outdated`; `current`, shortcuts, shims and the environment are unchanged.

gupkg never computes a hash after a download and writes it into `pkg.toml`; the hash it observed goes into the receipt only
(`gupkg pkg pin` copies it into `pkg.toml` on request, after showing where it came from). A later and separate `gupkg
update NAME` activates the new version. `check-updates` writes into the run's own root, so for a system package it runs
from an administrator shell.

**Providers** (`[track].provider`):

| Provider | Key fields |
| --- | --- |
| `github` | `repo`, `asset` or `asset_pattern` (regex with `(?P<version>...)`), `version_from = "tag"\|"asset"`, `tag_pattern`, `tag_prefix`, `release = "latest"\|"newest-matching"`, `prerelease` |
| `gitlab` | `project`, `link_name` or `asset_pattern` |
| `page` | `check_url`, `match` (regex with named groups), `select = "max"\|"first"\|"unique"`, `version`, `download`, `headers` |
| `index` | `check_url`, ordered `[[track.level]]` (`match`, `select`, `key`, `next`, `probe`) |
| `redirect` | `check_url`, `match`, `unwrap_query` |
| `json` | `check_url`, `version` (JSONPath subset), `download`, `filter`, `records`, `match`, `checksum`, `captures` |
| `last_modified` | `check_url`, `format = "%Y.%m.%d"` |
| `rss` | `check_url`, `match` |
| `git` | `repo_url`, `ref`; the version is `YYYYMMDD-HHMMSS-<short sha>` and the commit is the integrity anchor |
| `chain` | ordered `[[track.fetch]]` steps whose captures feed the next URL |
| `module` | `pkg.local/track.py` with `track(ctx) -> TrackResult` |

All providers share one HTTP client (User-Agent, timeout, retries, TLS, the `GITHUB_TOKEN` header for GitHub only). Matches
of a manifest's regular expression against fetched text run in a worker process under a deadline; a pattern that is too slow
is an error, never "no version found".

## Checksums

When a download has a `sha256`, it is verified and a mismatch always fails. When it has none, the call decides:

- the **`gupkg` command line** refuses, unless the call carries `--skip-checksum`, which covers only this call, only missing
  hashes and only `https://` addresses; the receipt records `skipped-cli` and `check` and `list --details` flag the install;
- **`gupkg-manager`** follows `[manager] checksum_missing` (`ask`, `refuse`, `skip`), reads the plan, and calls the CLI with
  `--skip-checksum=user-approved` or `=setting` for the packages it lets through;
- a version with `[source.installer]` is never skippable; a git source is verified by its pinned commit.

`gupkg pkg pin NAME@VERSION` copies the hash that a skipped install observed into `pkg.toml`, after showing the URL, the final
URL, the size and the date. Installs with no download at all are labelled "origin not verified" unless a registry
fingerprint matches (then "verified against registry"); a mismatch against the registry refuses the install.

## Hooks

`pkg.local\` holds trusted package-local Python modules (`PKG_MODULE_API = 2`): `track(ctx)` in a tracker, and `populate`,
`unpack` and `prepare` entry points elsewhere. A hook receives a typed context (`package`, `version`, `paths`, `http`,
`options`, `captures`, `reporter`) and may import only the standard library, what the gupkg bundle ships, `gupkg.hooklib`
and its own `pkg.local` folder; gupkg installs nothing for a hook. Version-directory scripts may use the network, because
Python cannot sandbox trusted code; an elevated run refuses a version directory or `pkg.local` that is not admin-only
before it imports anything.

## Development

Implementation and test guidance is in [_docs/development_guide.md](_docs/development_guide.md); the design that 1.0
implements is [_docs/rehaul_design.md](_docs/rehaul_design.md). The module overview is in
[src/gupkg/README.md](src/gupkg/README.md).
