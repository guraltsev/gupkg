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
- **Options go after the command**, except `--version`, `--help`, `--settings PATH`
  and `--sandbox DIR`.
- **Safe to repeat:** `install`, `update`, `check-updates` and `check --fix` can be
  rerun at any time; work already done is recognised and skipped.
- **Targets** are names (`firefox`, `firefox@135.0`, `user:firefox`, `main/firefox`)
  or paths. The command line has no `--all`; `gupkg-manager` is the program for
  batches.

## Install a program from the registry

```bat
gupkg registry sync                    :: once, then whenever you want a fresh catalogue
gupkg search editor
gupkg install vscode --mode user
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
gupkg install tool --mode user       :: %USERPROFILE%\opt, Start Menu and PATH of your account
gupkg install tool --mode system     :: C:\opt, all users; asks once and relaunches elevated
gupkg install tool --mode portable   :: settings and state in <package>\data; the folder can be carried
gupkg install tool                   :: auto: elevated shell -> system, otherwise user
```

An elevated gupkg never acts on a user or portable root: use a normal shell for those.
A portable root with `integrate = "none"` in its settings touches neither the registry
nor the Start Menu; `gupkg shell --mode portable` prints the `PATH` line to use.

## Install from a distribution folder or ZIP

A folder made by `gupkg pkg export` (or any `<name>\<version>\` folder with `pkg.toml`
and `install.cmd`) installs without gupkg being installed first, when it carries its own
gupkg:

```bat
<name>\<version>\install.cmd           :: double-click, or run; user mode
gupkg install C:\share\tool\1.0 --mode system   :: with an installed system gupkg, from an admin shell
```

The installed gupkg copies the folder into the mode's root (dropping the Mark of the
Web) and installs from the copy; `--in-place` installs where it stands.

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

Run both commands as a **normal user** in Task Scheduler (never with "highest
privileges": the programs refuse an elevated shell):

```bat
schtasks /Create /TN "gupkg check" /SC DAILY /ST 03:00 ^
  /TR "C:\bin\gupkg-manager.exe check-updates --all --json"
schtasks /Create /TN "gupkg update" /SC WEEKLY /D SUN /ST 04:00 ^
  /TR "C:\bin\gupkg-manager.exe update --all --yes --json"
```

For **system** packages `check-updates` from a normal shell only reports; an
administrator runs `gupkg check-updates <package>...` from an elevated shell (an elevated
`gupkg-manager` is refused, so a scheduled elevated task calls `gupkg.cmd` of the system
root with explicit package names) to write and prepare the new version in `C:\opt`. `update` of a system package asks for administrator rights once.
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
gupkg cleanup                          :: every root: non-current Apps, receipts, work folders, logs
gupkg cleanup --bundled                :: also drop bundled gupkg copies in version directories (where a root forwarder exists)
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
