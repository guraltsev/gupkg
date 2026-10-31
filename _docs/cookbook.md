# Cookbook

Short recipes for common administration tasks. Each one is self-contained.
Commands assume `gupkg` is on `PATH`; in scheduled tasks use the full path.

## Conventions used by every command

- **Exit status:** `0` success, `2` you or the configuration need to change
  something (including usage errors), `3` the operation failed, `4` internal error,
  `5` declined or input needed, `6` the package needs a newer gupkg. Scripts can rely
  on these alone.
- **`--json`:** prints exactly one JSON document on standard output (human text goes
  to standard error) with `api`, `command`, `ok`, `exit_code` and one entry per
  target. `--plan` shows what a call would do without doing it.
- **Options go after the command**, except `--version`, `--help`, `--settings PATH`,
  `--root DIR` and `--sandbox DIR`.
- **Safe to repeat:** `install`, `update`, `check-updates` and `check --fix` can be
  rerun at any time; work already done is recognised and skipped.
- **Targets** are names (`firefox`, `firefox@135.0`, `main/firefox`)
  or paths. The command line has no `--all`; `gupkg-manager` is the program for
  batches.

## Install a program from the registry

```bat
gupkg registry sync                    :: once, then whenever you want a fresh catalogue
gupkg search editor
gupkg install vscode
```

The registry is the separate repository
[`gupkg-registry`](https://github.com/guraltsev/gupkg-registry). `--offline` on
`search` uses only the last synced catalogue.

## Install a program from GitHub Releases

Create `<root>\<name>\_check_updates\pkg.toml` with a tracker (see
[Getting started](getting_started.md)), then `gupkg check-updates <name>` followed by
`gupkg install <name>`. `gupkg pkg new <name> --track github:OWNER/REPO` writes the
starter.

## User, system and portable installs

```bat
gupkg install tool                   :: normal shell: user mode, %USERPROFILE%\opt, Start Menu and PATH of your account
gupkg install tool                   :: administrator shell: system mode, C:\opt, all users
gupkg install tool-portable          :: a portable package: state in <package>\data, for one user
gupkg --root E:\opt install tool-portable   :: a root on a stick (user mode)
```

The mode is that of the shell, and a run never touches the other mode's root: use a normal
shell for your packages and an administrator shell for the all-users ones (the Start Menu entry
"gupkg manager (administrator)" opens the manager that way). A portable package is a package of
its own (`<name>-portable`, `modes = ["portable"]`) and installs for a single user only. A root
with `integrate = "none"` in its own `.gupkg\settings.toml` touches neither the registry nor the
Start Menu; `gupkg --root E:\opt shell` prints the `PATH` line to use.

## Install from a distribution folder or ZIP

A folder made by `gupkg pkg export` (or any `<name>\<version>\` folder with `pkg.toml`
and `install.cmd`) installs without gupkg being installed first, when it carries its own
gupkg:

```bat
<name>\<version>\install.cmd           :: double-click, or run; user mode
gupkg install C:\share\tool\1.0        :: with an installed gupkg: system mode from an admin shell, user mode otherwise
```

The installed gupkg copies the folder into the run's root (dropping the Mark of the
Web) and installs from the copy. Nothing is installed in place: to install where the folder stands,
make it a root of its own, `gupkg install <folder> --root <folder above the package name>`.

## See everything at a glance

```bat
gupkg list                         :: every package, version and status
gupkg list --outdated              :: a newer version is waiting
gupkg list --broken --details      :: problems, with the App kind and checksum notes
gupkg info firefox                 :: everything known about one package
gupkg check                        :: health: drift, App fingerprint, registry fingerprint
```

`list`, `info` and `check` read only the local disk and are instant. Asking upstream is
`check-updates`.

## Update everything

```bat
gupkg-manager check-updates --all      :: find and prepare new versions of every package
gupkg-manager update --all             :: show the plan, ask once, then activate them
gupkg-manager update --all --yes       :: no questions (for scripts)
```

A failing package never stops the others; each package's outcome is reported and the
exit status is the worst one. Re-running after fixing a failure picks up what is left.
For a handful of packages, name them: `gupkg check-updates a b c` then `gupkg update a b c`.

## Update unattended every night

Run both commands in Task Scheduler as the account whose packages they manage; a task that runs as a
normal user manages that user's root:

```bat
schtasks /Create /TN "gupkg check" /SC DAILY /ST 03:00 ^
  /TR "C:\bin\gupkg-manager.exe check-updates --all --json"
schtasks /Create /TN "gupkg update" /SC WEEKLY /D SUN /ST 04:00 ^
  /TR "C:\bin\gupkg-manager.exe update --all --yes --json"
```

For **system** packages (`C:\opt`) create the task with "Run with highest privileges": it then
works in system mode on the system root, and needs the gupkg installed in `C:\opt` (an elevated
run refuses any other gupkg tree). gupkg never asks for administrator rights itself.
gupkg never updates anything on its own; only a task you create does. A package whose
download has no checksum is refused in a schedule (`ask` behaves as `refuse`); see
[the checksum policy](operations.md#checksum-policy).

## Prepare now, switch during a maintenance window

```bat
gupkg-manager check-updates --all     :: now: download and verify; nothing changes yet
gupkg-manager update --all --yes      :: later: activate what is prepared
```

## Alert on problems from a script or CI job

```bat
gupkg check || echo gupkg found problems
gupkg list --outdated --json > outdated.json
```

## Roll back an update

```bat
gupkg switch <name> <older-version>
```

`switch` activates another local version and repairs the integration for it. Old
versions are kept until `gupkg cleanup`. The version you switch away from stays `available`.

## Repair a machine

```bat
gupkg install <name>                  :: re-create shortcuts, PATH, shims without downloading
gupkg install <name> --refresh        :: also rebuild the program files
gupkg check --fix                     :: repair what check reported
gupkg self repair                     :: gupkg's own launch metadata
```

## Adopt a program you already have

1. Put the files where gupkg expects them: `<root>\<name>\<version>\App\...`
   (for example `%USERPROFILE%\opt\notepad2\4.2\App`).
2. `gupkg pkg new <name>@<version>` writes a starter `pkg.toml` (a shipped `App`).
3. Edit it to add shortcuts, `PATH` entries or shims, run `gupkg pkg check <folder>`,
   then `gupkg install <name>`.

## Adopt a program that an installer unpacked (games)

1. Run the program's installer with `<root>\<name>\<version>\App` as the install folder (for example
   `C:\Games\Teardown\v1.0\App`).
2. Move the Start Menu shortcuts it made into `<root>\<name>\<version>\shortcuts\` (a folder inside the version directory, beside
   `App`; sub folders are fine).
3. In the version directory:

```bat
cd C:\Games\Teardown\v1.0
gupkg pkg adopt                        :: writes pkg.toml from the links, then installs in place
gupkg pkg adopt --no-install           :: only write pkg.toml; look at it, then `gupkg --root C:\Games install Teardown`
gupkg pkg adopt --force                :: replace an old-format pkg.toml (kept as pkg.toml.old)
```

The shortcuts go into the root's Start Menu folder, `integration.start_menu` (default `...\Start Menu\opt`). Give the games root
its own once, `gupkg --root C:\Games settings set integration.start_menu "%APPDATA%\Microsoft\Windows\Start Menu\Games"`. A
folder before each name is `adopt.shortcut_prefix` (empty by default) or `--prefix` for one call.

The root is the folder two levels above, so nothing is copied. Uninstallers, links that point outside `App` and links to
missing files are skipped, and each skip is listed with its reason. The root must be writable by your account, because
gupkg keeps its state in `<root>\.gupkg`.

## Pin a hash after a skipped install

```bat
gupkg install tool --skip-checksum     :: the download had no checksum; https only
gupkg pkg pin tool                     :: shows URL, final URL, size and date, then writes the hash into pkg.toml
```

## Uninstall

```bat
gupkg uninstall <name>                 :: removes shortcuts, shims, PATH entries, environment values
gupkg uninstall <name> --purge         :: also deletes the package folder and its data (asks first)
gupkg cleanup <name> --keep 2          :: drop old Apps, receipts and logs; never runs on its own
```

`uninstall` removes exactly what the install record lists.

## Free disk space

```bat
gupkg cleanup                          :: the run's root: non-current Apps, receipts, work folders, logs
gupkg cleanup --bundled                :: also drop bundled gupkg copies in version directories (where `gupkg-location` or `PATH` would find another gupkg)
```

Recoverable `App` folders are dropped; shipped ones and the active one never are.
`[cleanup] auto = true` in the settings makes `gupkg-manager update --all` clean up
afterwards.

## Read the logs

```bat
gupkg logs <name>                      :: list the logs of a package
gupkg logs <name> --path               :: print the newest text log
gupkg logs <name> --open               :: open it
```
