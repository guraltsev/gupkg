# Getting started

This walkthrough takes you from nothing to an installed, updatable program in
about five minutes. It uses [pandoc](https://pandoc.org/) as the example; any
program published as a ZIP on GitHub Releases works the same way.

## The idea in one minute

`gupkg` keeps every application in its own folder and describes how it should
appear in Windows in a small file called `pkg.toml`:

```text
C:\opt\pandoc\                 <- the package (the folder name is its name)
  _check_updates\              <- a tracker: how to find new versions (never installed)
  3.6.1\                       <- one folder per version
    pkg.toml                   <- what to put in Start Menu / PATH / etc.
    App\                       <- the program's files
  3.7.0\                       <- a newer version, side by side
  current  ->  3.7.0           <- a junction to the active version
```

Because versions sit side by side, an update never overwrites what is working,
and rolling back is one command. Checking for a new version and switching to it
are two separate commands, so nothing changes until you say so.

## 0. Install gupkg

Download `gupkg-bootstrap.cmd` from the repository and run it:

```bat
gupkg-bootstrap.cmd                  :: all users if the shell is elevated, otherwise just you
gupkg-bootstrap.cmd -Scope user      :: just you (from a normal shell; refused from an elevated one)
gupkg-bootstrap.cmd -Scope system    :: all users (elevated shell required)
```

It downloads the newest release ZIP, checks it against the SHA-256 in `RELEASES.md`
and installs gupkg like any other package. Then open a **new** terminal so the
`gupkg` command is on `PATH`. The details (mirrors, a ZIP from your own share, the
Windows security prompt that a downloaded ZIP can trigger) are in
[Installing gupkg](../README.md#installing-gupkg).

## 1. Check that it works

```bat
gupkg --version
gupkg --help
gupkg list
```

`gupkg --help` lists the commands and `gupkg <command> --help` documents every
option of one command. `gupkg list` is empty on a new machine.

## 2. Describe the program

Create the package folder and a `_check_updates` folder inside it with a tracker:

```bat
mkdir %USERPROFILE%\opt\pandoc\_check_updates
notepad %USERPROFILE%\opt\pandoc\_check_updates\pkg.toml
```

Paste this into `pkg.toml`:

```toml
format = 2
description = "Pandoc, a universal document converter"
project_url = "https://pandoc.org"

# How to find new versions: the project's GitHub releases.
[track]
provider = "github"
repo = "jgm/pandoc"
asset_pattern = 'pandoc-(?P<version>[\d.]+)-windows-x86_64\.zip'

# How to build the App of each version it finds.
[source]
download_url = "${asset_url}"
strip = "auto"

# Put the program on PATH and create a `pandoc` command.
[[path]]
value = "$App"

[[shim]]
name = "pandoc"
target = "$App\\pandoc.exe"
```

`$App` always means "this version's `App` folder"; you never type a
machine-specific path. A package can also declare Start Menu shortcuts
(`[[shortcut]]`) and environment variables (`[[env]]`); see the
[`pkg.toml` reference](../README.md#pkgtoml-reference-format-2). Many ready-made
definitions live in the [`gupkg-registry`](https://github.com/guraltsev/gupkg-registry)
repository, so you can often skip this step: `gupkg registry sync`, then
`gupkg install pandoc` (see the [cookbook](cookbook.md)). In `gupkg-manager` the same two steps are keys: `s` synchronises the registries and `a` lists the registry's packages
that are not installed yet.

## 3. Check the file

```bat
gupkg pkg check %USERPROFILE%\opt\pandoc
```

This validates everything without changing anything and reports *all* problems at
once. Add `--install` to also install into a throw-away root on a pretend machine.
Not sure what to write? `gupkg pkg new NAME --track github:OWNER/REPO` writes a
documented starter.

## 4. Find the newest version and install it

```bat
gupkg check-updates pandoc       :: writes <root>\pandoc\<version>\, downloads and unpacks it; activates nothing
gupkg install pandoc             :: activates it: junction, shim, PATH
```

`check-updates` runs the tracker, writes the new version directory with the
download's SHA-256 (GitHub publishes one) and prepares its `App`. `install` then
points `current` at it and creates the shim and `PATH` entry. Open a **new**
terminal (Windows hands new environment values only to new processes) and run:

```bat
pandoc --version
```

**Which mode?** A run has one mode, the mode of the shell you start it from: an elevated shell installs
for all users (`system`, in `C:\opt`) and any other shell installs for you (`user`, in
`%USERPROFILE%\opt`). There is no option to choose, and gupkg never relaunches itself elevated. To
install for all users, open an administrator shell and run the same command there; a package that
allows only the other mode is refused with a message that says how to run it.

`install` is safe to repeat: if a shortcut was deleted or `PATH` was reset, running it
again puts everything back without downloading the program. `gupkg install --plan pandoc`
shows what a call would do, and `gupkg check pandoc` reports what drifted.

**A download with no checksum.** If a version declares no SHA-256 and the tracker
could not discover one, gupkg refuses it. When you trust the source and it is an
`https://` address, add `--skip-checksum` to that one call. The receipt records
that nothing verified the download, and `gupkg pkg pin pandoc` can copy the observed
hash into `pkg.toml` afterwards.

## 5. Update

```bat
gupkg check-updates pandoc       :: is there a newer release? prepare it
gupkg list --outdated            :: which packages have one waiting?
gupkg update pandoc              :: activate the newest prepared version
```

The new version is downloaded, verified and unpacked into a fresh folder before
anything changes, so a failed download leaves the working version untouched. The old
version stays on disk. `update` never contacts the network for a package that has
nothing newer prepared: it says "up to date". To prepare now and switch later (a
maintenance window), run the two commands at different times.

For every package at once use the manager program:
`gupkg-manager check-updates --all` and `gupkg-manager update --all --yes`.

## 6. Roll back

Old versions are kept, so going back is one command:

```bat
gupkg switch pandoc 3.6.1
```

## 7. Uninstall

```bat
gupkg uninstall pandoc           :: removes shortcuts, shims, PATH entry and environment values
gupkg uninstall pandoc --purge   :: also deletes the package folder and its data (asks first)
```

`uninstall` keeps the version folders, so a later `install` brings the program
back. `gupkg cleanup` removes old `App` folders, receipts and logs, and never
runs on its own.

## Logs

Every run that acts on a package writes a text log and a `.jsonl` log into that
package's `.gupkg\logs\` folder, and a short run log into the root's
`.gupkg\logs\`. `gupkg logs pandoc` lists them, `--path` prints the newest and
`--open` opens it.

## Where next

- Many packages at once, schedules, settings and registries:
  [Operations guide](operations.md) and the [cookbook](cookbook.md).
- Something went wrong: [Troubleshooting](troubleshooting.md), which lists each
  error message with its fix.
- Every `pkg.toml` field: the [reference in the README](../README.md#pkgtoml-reference-format-2).
