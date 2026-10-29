# Manual smoke checklist

The automated suite covers behaviour through `FakeSystem` and fixtures. This checklist covers what it cannot: a real console, UAC,
the real registry, the Start Menu and the `PATH` broadcast, a real GitHub release, a machine without Python, and a copy of a real 0.17
tree. Each numbered entry says why it is manual. The entries are grouped by the issue that introduced them; the ids (for example
`033-6`) are what the closing notes of an issue refer to.

## How to run most of it without touching the machine

`gupkg --sandbox DIR COMMAND ...` runs the real command line against a pretend machine kept in `DIR`: environment values, `PATH`,
shortcuts and elevation are recorded in `DIR` (`system.json` and `.lnk.json` files) instead of the registry and the Start Menu, while
files and downloads are real. Put the package roots under the sandbox too, with `--settings DIR\settings.toml` naming them:

```bat
mkdir %TEMP%\s
gupkg --sandbox %TEMP%\s settings init
:: edit %TEMP%\s\settings.toml: [roots] user = '%TEMP%\s\opt' (and system)
gupkg --sandbox %TEMP%\s --settings %TEMP%\s\settings.toml install <package>
type %TEMP%\s\system.json
```

Use it for every check below that only needs to see what gupkg *would do to the machine*. Only the checks listed under "Real Windows
only" need the real thing.

## Real Windows only: the short list

These are the checks that no sandbox or fake can replace.

- **UAC and elevation:** entries 025-1 to 025-3, 029-2, 029-3, 030-2, 030-3, 033-1 and 033-3.
- **The Start Menu, the registry and the `PATH` broadcast:** 021-1, 021-2 and 028-1.
- **A real GitHub release and a real second version:** 016-5, 033-6 and 032-3.
- **Smart App Control and the Mark of the Web:** 033-7.

## Documentation and the 1.0 release (issue 032)

1. **032-1. Removed.** The migration tool no longer exists (issue 035 T01), so there is nothing to check here.
2. **032-2. Documentation reads true.** On a machine where gupkg 1.0 is installed, follow
   [Getting started](../_docs/getting_started.md) from step 0 to step 7 literally (pandoc, user mode), then the
   [cookbook](../_docs/cookbook.md) recipes "Update everything", "Roll back an update" and "Uninstall", and the scheduled-task
   example of "Update unattended every night" (as a normal user). Every command must behave as written and every message in
   [Troubleshooting](../_docs/troubleshooting.md) that you meet must match. Why manual: it needs a network, a real
   GitHub release and Task Scheduler. (The links and the README command list are checked by
   `tests/test_documentation.py`.)
3. **032-3. `self check-updates` then `self update`, 1.0.0 to 1.0.1.** With 1.0.0 installed from the bootstrap, publish a
   1.0.1 test release (a throwaway fork: `scripts\bump.cmd 1.0.1`, `scripts\release.cmd "test"`; point the tracker at the fork
   by testing with the fork's `RELEASES.md`), run `gupkg self check-updates` and then `gupkg self update`. Expect: a `1.0.1` version
   directory with a populated `App` after the check (the ZIP was verified against its `RELEASES.md` row and the tree against
   its `MANIFEST.sha256` before anything was unpacked), `current` and `gupkg --version` still 1.0.0 until `self update`, then
   1.0.1 everywhere, the old version directory untouched, and `gupkg self status` healthy. Also confirm gupkg's own package has no
   `download_url` (`gupkg info gupkg`). Why manual: it needs two real releases, the junction switch and the real shims. (The
   verification rules are covered by `tests/test_self_release_update.py`.)
4. **032-4. The 1.0 release itself.** After `scripts\bump.cmd 1.0.0` and `scripts\release.cmd "<notes>" --no-push`, on a clean VM
   run the built ZIP's `install.cmd` (or `gupkg-bootstrap.cmd -Source <zip> -Sha256 <digest>`), then `gupkg --help`, `gupkg --version`
   and `gupkg self status`. Confirm `RELEASES.md` has the new row with the digest of that exact ZIP and that nothing was pushed. Why
   manual: it publishes a real artifact and needs a clean machine.

## One self-contained release ZIP (issue 016)

Each step needs a real Windows machine, a real GitHub release, UAC, or a
machine without Python, which the automated tests cannot provide. The automated
tests cover the build, the manifest check, the bootstrap with `-Source`, the
update hooks and the removed commands; these steps cover live downloads and
activation.

1. **Fresh VM, no Python, user scope** (needs two published releases, or the
   first one with its `RELEASES.md` row). On a Windows VM without any Python,
   run `gupkg-bootstrap.cmd` with no arguments from a non-elevated prompt. Watch
   the network (for example with a proxy log): the only downloads are
   `RELEASES.md` and `gupkg-<version>-win-amd64.zip`. In a new terminal,
   `gupkg --version` prints the release version. Then confirm
   `gupkg self status` reports a healthy install (the installed `Icons`,
   `Shortcuts` and `App` folders must not be listed as extra files). Why manual: it needs the real GitHub asset, the registry
   `PATH` change and a machine with no Python.
2. **Same VM, elevated, `-Scope system`.** From an elevated prompt run
   `gupkg-bootstrap.cmd -Scope system`, open a new elevated terminal, and run
   `gupkg --version` and `gupkg self status`. Both succeed, and the
   elevated run reports no untrusted folder. Why manual: it needs a real
   administrator token and the `C:\opt` ACL change.
3. **Self-update.** With release N installed and release N+1 published, run
   `gupkg self check-updates` and then `gupkg self update`. Confirm the new version folder appears beside the
   old one with its own `gupkg\python\`, `gupkg\MANIFEST.sha256` verifies
   (`gupkg self status` is healthy), `current` points at it, and the old
   version folder is untouched. Why manual: it needs two real releases and the
   junction and shim activation.
4. **Edited file.** In the installed version folder change one file (for
   example append a line to `gupkg\README.md`), then run
   `gupkg self status`. It exits with status 3 and names that file as
   modified. Why manual: the automated test uses an unpacked copy, not an
   activated install with the real shims.
5. **Publishing.** From a clean tree on a throwaway fork run
   `scripts\release.cmd "test release"` with `gh` signed in. Confirm the tag
   `v<version>` and the release asset appear, and the new `RELEASES.md` row holds
   the digest that `Get-FileHash` reports for the downloaded asset. Why manual:
   it creates a real GitHub release.

Shortcut and junction checks (issue 017):

1. Install a package with shortcuts, then open each shortcut's Properties
   dialog in Explorer. Confirm Target, Start in, Comment and the icon match
   `pkg.toml`. Automation cannot open the Properties dialog.
2. Run the same install from `gupkg tui` (worker thread) and from an elevated
   system-scope shell. Confirm the shortcuts appear under the matching Start
   Menu root. Elevation and the TUI need a real session.
3. Install a 10-shortcut package before and after the change and compare
   times (automated measurement: 6.75 s before, 0.15 s after for 10 shortcuts).

One interpreter (issue 018):

1. On a machine with a system Python 3.13 on `PATH` (and `py` installed), install
   a release ZIP and run `gupkg self status`. Confirm `sys.executable`
   is the bundled `...\gupkg\python\python.exe`. Run the same command from an
   elevated shell and confirm the same interpreter. Why manual: needs a real
   machine with other Pythons and a real administrator token.
2. In a fresh clone, run `scripts\test.cmd` before creating `.venv` (expect exit
   9009 and the creation command), then create `.venv` from `requirements-dev.txt`
   and run it again. Why manual: needs a network install of the dev packages.

## A bundled Python without pip (issue 019)

These steps need the network (PyPI and python.org), a real build, and a clean VM,
which the automated tests avoid: they build offline with stand-in wheels and run
the probe on the committed runtime.

1. **Real build and probe.** In a dev checkout whose `.venv` has pip, run
   `scripts\build-standalone.cmd` (network needed for the pinned CPython and the
   locked wheels). The build prints no probe failure and writes the ZIP. Unzip it
   into an empty folder and confirm `gupkg\python\Lib\site-packages` has no `pip*`,
   `setuptools*`, `_distutils_hack` or `bin` entry and does have `tomli_w`. Why
   manual: the real wheels and the real CPython archive come from the network.
2. **No pip in an installed release.** On the clean VM of the issue 016 steps,
   run `gupkg\python\python.exe -I -m pip --version` from the installed version
   folder. It fails with `No module named pip`. Then run an install, an update,
   `gupkg manager`, `gupkg tui` and the package TUI from the installed release;
   all work. Why manual: it needs the real release ZIP on a machine without Python.
3. **Refreshing the runtime.** In the dev checkout run
   `.venv\Scripts\python tools\update_deps.py --repair`. It reports
   `Dependencies repaired`, `git status` shows no change under
   `src\gupkg\python`, and the folder has no `pip*` entry. Why manual: it
   downloads the locked wheels from PyPI.

## Format 2 and settings layers (issue 023)

The automated tests cover the model, the validator, the settings layers (with the
admin-only check replaced by a stand-in). These steps need a real administrator token.

1. **Elevated launchers refuse.** Take an installed version directory, open an administrator shell, and run the generated
   `install.cmd` and `gupkg-tui.cmd` in it. Each prints "This launcher does not run
   elevated" and exits 1 without starting any gupkg. Run them from a normal shell and
   confirm they start the bundled gupkg, else the one named by `<root>\.gupkg\gupkg-location`, else `gupkg` on `PATH`. Why manual: the
   automated launcher tests run in a normal shell, and elevation needs a real token.
2. **Elevated settings layers.** In an administrator shell, with a `settings.toml` placed
   in `<user root>\.gupkg\` that sets `ui.color = "never"`, run the command that shows the
   settings once issue 029 adds it, and confirm the user layer is ignored. Then point
   `--settings` at a file outside `C:\opt` and confirm it is ignored, and at a file inside
   `C:\opt\.gupkg\` and confirm it is used. Also make `C:\opt\.gupkg` writable by `Users`
   and confirm the elevated run refuses the layer. Why manual: it needs a real
   administrator token and real access-control lists (automation uses a stand-in check).
3. **Removed.** The migration tool no longer exists (issue 035 T01).
## The system boundary and sandbox mode (issue 021)

The contract tests run the real `WindowsSystem` against throwaway locations. These
steps cover what they deliberately leave out: the real registry, Start Menu, a UAC
prompt and the broadcast to running programs.

1. **Sandbox leaves the machine alone.** Note the `PATH` of the User environment
   (`[Environment]::GetEnvironmentVariable('Path','User')`) and the contents of
   `%APPDATA%\Microsoft\Windows\Start Menu\opt`. Run
   `gupkg --sandbox %TEMP%\s install <fixture package with a shortcut,
   an [[env]] row and a [[path]] row>`. Confirm the `PATH`, the HKCU
   environment values and the Start Menu folder are unchanged, and that
   `%TEMP%\s\system.json` lists the environment value and `PATH` entry while a
   `.lnk.json` record exists below `%TEMP%\s`. Run the same install a second time and
   confirm `system.json` keeps the first run's log. Repeat after setting
   `"elevated": true` in `system.json` (the sandbox never reads the real token) and
   confirm the HKLM environment and `%ProgramData%` Start Menu are unchanged too.
   Limits to expect: default package and config roots are not redirected (a package
   given by path is installed where it is), and only `git` (on `PATH`) and the bundled 7-Zip may
   run as child processes. Why manual: it compares against the real registry and Start Menu, which
   the automated tests never read.
2. **Real environment broadcast.** After a normal user-scope install of a package with
   a `[[path]]` row, open a new terminal and confirm the entry is on `PATH`; then run
   `gupkg self repair` and confirm no error mentions the broadcast. Why manual:
   `WM_SETTINGCHANGE` reaches every open window, so the tests only exercise it through a
   mocked `user32`.
3. **Elevated relaunch through the launcher.** (Retired by issue 035 T07: gupkg starts no elevated process; the manager is
   started elevated by its own Start Menu shortcut, see `035-T07-1`.)
4. **Administrator detection.** In an elevated and a non-elevated shell run
   `gupkg self status` and `gupkg install <package>`; confirm
   the elevated shell selects system mode and the other user mode. Why manual: needs
   real tokens.

## Install modes (issue 025)

The automated tests resolve modes, contexts and the elevation step on a `FakeSystem`. These steps
cover the parts that need a real token, a real UAC prompt, Explorer's marking or a second account.
The engine and the command line that call these functions arrive with issues 028 and 029, so the
steps that need `gupkg install` are run once those land.

1. **Real relaunch from the system gupkg.** (Retired by issue 035: gupkg no longer starts an elevated copy of itself; the
   mode of a run is the mode of its own token, see `035-T05-1`.)
2. **A user-writable gupkg never elevates.** (Retired by issue 035 with the relaunch; the case that remains is an elevated run
   from a user-writable gupkg, see `035-T05-2`.)
3. **Elevated shell and the user root.** (Retired by issue 035: an elevated run has one scope, the system's; there is no
   `--mode` and no `user:` prefix to refuse, see `035-T05-1` and `035-T05-3`.)
4. **Distribution from an administrator shell.** Extract a distribution ZIP with Explorer (every
   file gets a Mark of the Web), then from an elevated shell run
   `gupkg install <folder>\<name>\<version>` (no flag: the elevated token is the system mode). Confirm the folder is copied into
   `C:\opt\<name>\<version>`, that `Get-Item -Stream * <file>` shows no `Zone.Identifier` on any
   copy, and that the receipt says the source was marked. Why manual: Explorer does the marking.
5. **Portable in place** (retired by issue 035: a portable package is an ordinary user package, see `035-T02-1`).

## Event stream, console and logs (issue 022)

The automated tests render with a recording Rich console and write logs below pytest
temporary directories. These steps cover real consoles, real permissions and a real TUI.

1. **Real terminal rendering.** In Windows Terminal, then in a classic console window
   (`conhost`), run `gupkg install <package with a download_url>`. Confirm
   coloured phase rules, a ✓ or ✗ with the time, a download bar that disappears and leaves one
   line, and a closing summary that names the logs and is the last thing printed. Run it again
   with `set NO_COLOR=1` and with output redirected (`> out.txt`); `out.txt` has `==>` headers,
   no escape characters and no carriage returns (`findstr /R "\x1b" out.txt` finds nothing).
   Why manual: terminal modes and fonts cannot be asserted offline.
2. **System logs are admin-only.** From an elevated shell run `gupkg install
   <package>`. In a normal shell confirm `icacls C:\opt\.gupkg\logs` and
   `icacls C:\opt\<name>\.gupkg\logs` show write access only for administrators and SYSTEM,
   and that a standard user can neither create nor modify a file there. Why manual: needs
   a real admin-only root.
3. **A standard user cannot steer an elevated log write.** As a standard user prepare
   `<user folder>\events.jsonl` and a junction `mklink /J <user folder>\link C:\Windows\Temp`.
   From an elevated shell run `gupkg install <package> --events-file ..\link\x.jsonl` and
   `gupkg install <package> --events-file C:\Windows\x.jsonl`; both are refused (exit 2) and
   nothing is created outside the log directories. Why manual: UAC cannot be automated.
4. **Nothing behind the TUI.** Open `gupkg tui` on a package whose `[source]` is an
   unreachable Git URL and run Install. The screen shows git's error lines inside the Fetch
   phase and no text appears outside the TUI. Quit and confirm
   `<root>\<name>\.gupkg\logs\` holds the `.log` and `.jsonl` of that run and the screen's
   last line named the log. Why manual: depends on a real terminal under Textual.
5. **Non-UTF-8 console output.** In a console using a legacy code page (`chcp 437`) run an
   install whose origin script prints accented text and use `7z` output; confirm the lines are
   readable or replaced, never a Python traceback. Why manual: needs real code pages.

## Built-in services (issue 026)

The automated tests replay synthetic HTTP fixtures, build archives at test time and run
7-Zip for real, but they cannot reach vendor sites or build NSIS installers. These steps cover
what only real sites and real installers show.

1. **Re-record the HTTP fixtures (owed to issue 031).** The files under `tests/fixtures/http` are
   synthetic fixtures modelled on the registry hooks' regular expressions, marked
   `"recorded": null`, because the worker had no network. With a network, run `python
   tools/record_http_fixtures.py`: it runs each tracker against the live site and records the whole
   walk, the date and the `expected` block. Review the diff (a changed `expected` is a vendor
   change, a failing case is a pattern that was wrong) and commit the recorded files. Why
   manual: needs the real vendor sites.
2. **Real NSIS installers.** In a sandbox (`gupkg --sandbox <dir> install`) install the
   obsidian and pidgin trackers of `tests/fixtures/registry_100` after copying them into a test
   registry with a real downloaded installer (`kind = "nsis"`). Confirm `App` holds the
   application with no `$PLUGINSDIR` folder and no uninstaller, and that no installer code ran
   (no new Start Menu or Programs entry). Why manual: no NSIS sample can be built offline and
   running a vendor installer is never done by the tests.
3. **Real self-extractor and tarball.** Do the same for firefox (`kind = "sfx"`, `subdir =
   "core"`) and msys2 (`.tar.xz`, `strip = 1`). Confirm `firefox.exe` and `usr\bin\bash.exe`
   start. Why manual: needs the real vendor archives.
4. **A real Git source.** Run a tracker with the `git` provider against a public repository and
   install the version it writes: the commit in `[source.git]` equals `git ls-remote`, and `App`
   has no `.git` folder. Why manual: the tests use a scripted `git`.
5. **Resumed download.** Start a large `check-updates` download, cut the network, restore it and
   repeat the command: the progress starts from the earlier size (the cache folder holds one
   `.part` file that disappears). Why manual: needs a real flaky connection.

## Execution engine (issue 028)

The engine has no command line until issue 029, so these steps call it from Python
(`python -c "from gupkg.engine import lifecycle ..."` on a real `WindowsSystem`, in a throwaway
root such as `%TEMP%\gupkg-028`). Everything the tests do on `FakeSystem` is repeated for real here.

1. **Real integration, install and uninstall.** On a throwaway user root install a package that
   declares a shortcut, a shim, an environment value and a `PATH` entry, then uninstall it. In Explorer
   the Start Menu entry appears and disappears, `where tool` finds the shim and then not, `reg query
   HKCU\Environment` shows the value and the entry and then neither, and one `WM_SETTINGCHANGE`
   reaches a new `cmd`. Why manual: real Start Menu, registry and broadcast.
2. **Elevated `check-updates` and the trust checks.** From an admin shell on a real `C:\opt` run
   `check-updates` for a system package: it writes and populates the new version in `C:\opt\<name>`
   and leaves `current` alone. Repeat from a normal shell: it only reports and `C:\opt` is unchanged.
   With the interpreter folder made user-writable (`icacls`), the elevated run refuses before it
   starts the regex worker. Why manual: UAC and real ACLs cannot be automated.
3. **Removed.** The migration tool no longer exists (issue 035 T01).
4. **Portable state across an update.** Install a real portable application (for example a
   SumatraPDF zip with `persist = ["SumatraPDF-settings.txt"]`), change a setting, `update` to the next
   version and `switch` back: the setting survives. Why manual: needs a real application that
   writes its own settings.
5. **Mark of the Web.** Install a distribution folder that Explorer extracted from a downloaded
   ZIP: `gupkg check` shows the install as `origin not verified`, the copies in the root have no
   `Zone.Identifier` stream (`dir /r`), and the receipt says `source_marked = true`. Why manual: the
   mark is written by Explorer.
6. **Cancel between packages.** Start a three-package batch with a slow download in the first and
   create the stop marker while it runs: package 1 finishes and is installed, 2 and 3 are
   reported as skipped. Why manual: timing with a real network.
7. **A real vendor installer (GIMP).** With a pinned GIMP version (`[source.installer] run =
   ["/VERYSILENT", "/CURRENTUSER", "/NOICONS", "/DIR=$Stage"]`) and `hooks.allow_vendor_installers = true`,
   install it into a throwaway user root: the output says `runs vendor installer`, `App` holds the
   installed program and `current` points at it. Repeat with the setting `false`: nothing is downloaded or
   run. In an elevated run, make the work folder user-writable (`icacls`) and see the installer refused
   before it starts. Why manual: a real Inno Setup installer and real ACLs; the tests never run one.
8. **One run per package.** Start `install` of a large package in one console and, while it downloads,
   `install`, `cleanup` and `update` of the same package in a second one: the second `install` and `update`
   fail with `package-busy` naming the first run, `cleanup` skips the package and the first run's
   `.gupkg\work` folder survives. Kill the first run (Task Manager) and run again: the stale lock is
   reclaimed. Why manual: two real processes and a real kill.

## Registry 2 (issue 031)

Everything the registry work could prove offline is automated (conversion, validation, the registry tool against recorded
responses, fingerprints). The agents had no network, so these steps are the owner's. The registry lives in the separate
repository `gupkg-registry`; its README repeats the commands.

1. **First population of the registry (needs the network).** In a checkout of `gupkg-registry` (branch merged), run
   `_scripts\registry-tool.cmd update --sandbox C:\temp\registry-sandbox`. Each package prints `added`, `current` or `failed`.
   Fix failing trackers (the pattern or the vendor page changed) and run `update <name>` again. Review every new
   `<name>\<version>\` directory, especially each one marked `[REVIEW]` (`sha256_from = "registry"`: the vendor publishes no
   checksum, so the hash is the tool's own over an `https://` download: confirm it against the vendor's page or a second
   download), then `_scripts\validate.cmd`, commit and push. Why manual: needs the vendors' sites; invented hashes must never
   be committed.
2. **Hash the legacy version.** Run `_scripts\registry-tool.cmd pin qbittorrent/5.2.3`; confirm `unpinned` disappears from
   `_registry.toml` and `validate` stays green. Why manual: needs SourceForge.
3. **`--auto-push` on a scratch remote.** Clone the registry into a scratch folder with a bare repository as `origin`, run
   `registry-tool update <name> --auto-push` for a package with a new version and check the last commit (only the new
   directory and `_registry.toml`) and that the bare repository has it. Why manual: needs a real source-control client and
   credentials; the tests only record the commands.
4. **Every package installs under the strict default.** On a clean Windows runner (or VM), with the registry synced, run
   for each package `gupkg --sandbox <dir> install <name>` (no `--skip-checksum`), once in user mode and, for the
   portable-capable ones (see `PORTABLE-UNVERIFIED.md`). Expect exit 0, `App` filled, and for
   versions with `sha256_from = "registry"` the label *verified*. Why manual: needs the engine (issue 028), a clean Windows and
   the vendors' downloads.
5. **Verify each portable mechanism.** For every row of the registry's `PORTABLE-UNVERIFIED.md`: install portable, change a
   setting, update or reinstall, and check it survived and that nothing was written outside `<package>\data` and `App`.
   Correct the package's `[portable]` table where the upstream behaviour differs and delete its row. Why manual: only the real
   applications show how they enter portable mode.
6. **Record the HTTP fixtures and commit a real NSIS sample** (owed by issue 026 to this issue): see "Built-in services" steps 1
   and 2. After recording, `python tools/snapshot_registry.py <registry> --check` must still say "snapshot is current", and the
   CI job of the registry runs the recorded cases against its own trackers.
7. **Distribution folder against the published fingerprint (needs the engine, issues 028 and 029).** Copy a registry version
   directory (for example `<registry cache>\qbittorrent\5.2.3`) to a folder outside every root and run `gupkg install <folder>
   ` with the registry synced: the install is labelled *verified against registry <name>*. Change one byte of the
   copy's `pkg.toml` and repeat: the install is refused and names `pkg.toml`. The comparison itself is automated
   (`tests/test_fingerprint.py`); what is manual is the engine calling it. Why manual: the install engine does not exist yet.
8. **GIMP's vendor installer.** The tracker declares `[source.installer]` (Inno Setup, `/DIR=$Stage`). Confirm the engine runs it
   (no code that runs `[source.installer]` existed when this was written) and that `require = ["bin/gimp-3.exe"]` is judged
   after the installer ran, not before. Why manual: real installer.
9. **Git trackers over SSH.** latex-toolkit and overleaf-sync use `git@github.com:...` addresses (as in 0.17). The git service
   runs with a scrubbed environment, so check that `registry-tool update latex-toolkit` can reach them from the maintainer's
   machine, or switch them to `https://` if the repositories are public. Why manual: depends on your SSH setup.
10. **CI.** Push the registry branch and confirm the `validate` workflow is green on `windows-latest` (it checks out
    `guraltsev/gupkg`, so the gupkg code of this branch must be on that repository's `main` first, or run it by hand with
    `gupkg_ref`). Replace the two actions' version tags by commit SHAs of those tags (not verifiable offline). Why manual:
    GitHub only.
11. **msys2's real `.tar.xz` through the safe unpacker.** Run `gupkg --sandbox <dir> install msys2` (or the tool's `update msys2`)
    with the real release: it must unpack and `usr\bin\bash.exe` must start. The safe unpacker refuses any link entry, so if the
    real archive contains one the unpack fails and the package needs another route. Why manual: needs the real archive.
12. **GitHub digests.** For every package whose tracker uses the `github` provider (about 30), confirm the release assets really
    carry a `digest` (the registry tool's output shows `github-digest` rather than `[REVIEW]`). A package whose releases carry
    none gets a registry-computed hash instead: decide whether to accept it. Why manual: needs the live API.
13. **The shutter-encoder page.** Run `registry-tool update shutter-encoder` and check which of the page's links was kept (the
    Windows 64-bit file) and how many HEAD requests it took. Why manual: the real page decides.
14. **Windows device-name versions.** Windows cannot create a folder named `CON` through normal calls, so the tests only
    cover the rule. Check on a real machine that a registry holding a folder named `con` or `Nul.txt` fails
    `registry-tool validate` with a clear message (create it with `mkdir \\?\C:\path\con`), and that nothing in gupkg lists it as a
    version. Why manual: needs the real Win32 namespace.


## The unified command line and machine interface (issue 029)

Run these with a gupkg built by `scripts\build-standalone.cmd`, unpacked into a throwaway root (`%TEMP%\gupkg-029`), unless a step
says otherwise. They repeat on a real machine what `tests/test_cli_*.py` do on `FakeSystem`.

1. **A real console.** In Windows Terminal and in a redirected `cmd` (`gupkg install <name> > out.txt`), run `gupkg install`,
   `gupkg update <name> --plan`, `gupkg list --details` and `gupkg info <name>`. On the terminal the phases are coloured and the
   download has a live bar; redirected, the output is plain (`==> Phase`, no colour, one final progress line) and `--json` leaves
   standard output a single document. Why manual: only a real console shows colour and the bar.
2. **The real UAC relaunch (needs the system gupkg).** (Retired by issue 035 T07: no relaunch, no `--stop-file`; the stop marker named in
   `run_start` still stops a run, see `035-T07-2`.)
3. **A user-writable gupkg never elevates.** (Retired by issue 035 T07; see `035-T05-2`.)
4. **The elevated child refuses a user folder.** (Retired by issue 035 T07: there is no child; an administrator shell copies a distribution
   folder into `C:\opt` and a normal shell refuses a path outside its root.)
5. **The pass-through commands.** `gupkg tui <folder>` opens the package view of that folder and `gupkg manager` opens the browser (from a normal
   shell the user root, from an elevated shell the system root; neither refuses). Check that the exit code of each program is the exit code of `gupkg`. Why
   manual: needs the real bundle layout and a real console.
6. **`gupkg logs`.** `gupkg logs <name> --open` opens the newest log in the program Windows associates with `.log`; `--path` prints
   its path. Why manual: needs the Windows shell association.
7. **`gupkg self status`.** On an installed gupkg expect `ok` and the bundled interpreter; edit one file below the version
   directory and expect exit 3 naming it. Why manual: needs a real install.
8. **`gupkg pkg check --install` with real downloads.** On a package from the registry (a version directory with its pinned hash)
   expect exit 0 with the real network, and the real user root and registry unchanged. Why manual: real downloads.
9. **`gupkg shell`.** With `[integration] integrate = "none"`, `for /f "delims=" %i in ('gupkg
   shell') do %i` puts `<root>\bin` on that console's `PATH`. Why manual: `cmd` interprets the line.
10. **`uninstall --purge` on a real machine.** After a purge the package folder is gone including this run's own package log (it
    cannot stay open), and the root's run log under `<root>\.gupkg\logs` records the deletion. Why manual: only real file locking
    shows whether the log files were released.
11. **A hostile target under real UAC.** (Retired by issue 035 T07: no command line is built for an elevated child.)
12. **The parent waits for the elevated child.** (Retired by issue 035 T07: there is no elevated child.)
13. **Redirected output with non-ASCII names.** Make a package folder `жук` in the user root, then run `gupkg list > out.txt`,
    `gupkg list --json > out.json` and `gupkg list | more` in `cmd`: exit code 0, `out.txt` is UTF-8 (open it in an editor that
    shows the encoding), `out.json` is pure ASCII with `\u` escapes that parse back to `жук`. In a console set to a legacy code
    page (`chcp 437`) the listing shows replacement marks for what the page cannot draw, with no error. Why manual: only a real
    console has a code page.

## The UI programs (issue 030)

1. **Look at both programs in a real terminal.** From a normal shell run `gupkg-manager` in Windows Terminal and in a plain `cmd` window
   (80x24 and maximised) and open a package with Enter. Expect a one-line key-hint list on top and a one-line status line at the
   bottom on every screen (browser, package view, run view, each dialog, help), nothing drawn over them, no flicker when opening a package
   and coming back, and the filter and selection kept. Repeat with `set NO_COLOR=1`: every state is still readable in words. Why
   manual: only a real terminal shows rendering, colour and resize behaviour.
2. **Update all with real elevation.** With the system gupkg installed and one outdated user package and two outdated system packages,
   run `gupkg-manager update --all` (type `y`): expect one plan, one question, one UAC prompt for both system packages and the user
   package updated first. Decline the UAC prompt: the system packages are unchanged and the run ends with exit code 5. Why manual: real
   UAC and the real second process.
3. **Cancel during an elevated run.** Start an update of several system packages and press Escape then `y` while the first one runs:
   it finishes cleanly, the rest are reported skipped and the exit code is 5; no further UAC prompt appears. Why manual: needs the
   elevated child's stop marker in a real temporary folder.
4. **Run from a user copy.** From a bundled or user-mode gupkg run `gupkg-manager update --all --yes`: system packages are reported as
   needing the system gupkg, no UAC prompt appears, user packages are updated. Why manual: the real ACL check decides it.
5. **Schedule behaviour.** In Task Scheduler (no console) run `gupkg-manager check-updates --all` and then `gupkg-manager update --all
   --yes`: both finish without waiting for a key, and a package without a checksum under the default `ask` is reported as `refused: no
   checksum` with exit code 3. Then tick "Run with highest privileges" on the task: it is refused with exit code 2 (the programs refuse an
   elevated shell). Why manual: needs a real scheduled task without a terminal and a real elevated token.
6. **A hostile package name.** Make a folder `a&calc` in the user root and run `gupkg-manager update --all --yes` and open the browser:
   the package is listed literally, refused alone with exit code 2, and no Calculator starts. Why manual: real `cmd` quoting.
7. **Closing while a call runs.** Start `gupkg-manager update --all --yes` of several packages, then close the console window (and, separately, press Ctrl-C in
   the browser's run view): no `python.exe` of that gupkg is left in Task Manager after a few seconds, and the log shows the run stopped after its current
   package. Why manual: real process trees and a real window close.
8. **After the program closes.** Close `gupkg-tui` after an update: the console shows the summary and the log path (and nothing with
   `[manager] keep_screen = false`). Why manual: real console behaviour after a full-screen program exits.


## Bundled mode, package launchers and bootstrap (issue 033)

1. **Elevated launcher refusals and the elevated test run.** From an elevated PowerShell run
   `scripts\test.cmd tests\test_package_launcher.py -v`: the twelve tests marked `needs_admin` (they skip in a standard shell) build an
   admin-only tree under the temporary folder, grant `Users` write on one object at a time (package folder, version folder, root data
   folder, `gupkg-location`, a Python file, the root itself, a file deep in the installed gupkg) and expect the launcher to refuse, and expect
   it to proceed in the untouched tree and through `current` only when `current` names a version of the same package. Then right-click
   an `install.cmd` in `Downloads` and choose Run as administrator: expect `Refusing to run elevated` and no Python started. Why manual:
   UAC and the integrity level of a real elevated token (the automated tests simulate "elevated" to run the real access check).
2. **A bundled install leaves no gupkg behind.** On a machine with no installed gupkg, run `gupkg pkg export` for a small package (or use a
   registry package with `gupkg pkg bundle`), unzip the ZIP into `Downloads` with Explorer and double-click `install.cmd` (click Run on
   the security warning). Expect the program installed under `%USERPROFILE%\opt\<name>\<version>`, its shortcut and shims, and no
   `<root>\gupkg` package, no `<root>\.gupkg\gupkg-location`, no `gupkg` shim and no gupkg `PATH` entry; nothing new under `%LOCALAPPDATA%`,
   `%APPDATA%` or `%ProgramData%` other than the Start Menu entry. Delete the `Downloads` folder: the program still works. Then
   double-click `current\gupkg-tui.cmd`, run check-updates and an update, and confirm the new version directory holds a copy of `gupkg\`
   and that switching back to the old one leaves its launchers working. `uninstall --purge` leaves only `<root>\.gupkg\logs`. Why manual:
   the real registry, Start Menu and a clean machine (clean-VM run; the automated tests use `FakeSystem`).
3. **Bootstrap, user and system.** On a clean VM run `gupkg-bootstrap.cmd` (user), then in an elevated shell
   `gupkg-bootstrap.cmd -Scope system`. Expect `gupkg\<version>` in `%USERPROFILE%\opt` and `C:\opt`, `App` matching `MANIFEST.sha256`,
   `current`, the three shims, `PATH`, the Start Menu entry and `<root>\.gupkg\gupkg-location`; `C:\opt` and `C:\opt\.gupkg` are
   admin-only (`icacls`); `gupkg check` is healthy. Re-run both: nothing breaks. Why manual: the real ACLs, junction, registry and UAC.
4. **Squatted system root.** As a standard user create `C:\opt` (`mkdir C:\opt`), then run `gupkg-bootstrap.cmd -Scope system` elevated:
   expect a refusal naming the reason and nothing unpacked; delete the folder and run it again to see it succeed. Why manual: needs a real
   `C:\opt` created by a standard user (the automated test checks the same decision on a temporary folder).
5. **The bundled gupkg runs first; the root's gupkg-location and PATH come next (issue 035, T11).** With bundled installs from item 2 in the root,
   run the bootstrap in the same root: their `install.cmd` and `gupkg-tui.cmd` still run their bundled copy (see the newest file in
   `<root>\.gupkg\logs`). Rename a bundled `gupkg` folder: the launcher runs the gupkg named by `<root>\.gupkg\gupkg-location` (and prints a
   `Skipped ...` line instead if that file is missing or wrong). Then temporarily set `requires_gupkg = ">=99"` in the version's
   `pkg.toml`, run `install.cmd`: the bundled gupkg exits 6 and the launcher goes on to the gupkg named by `gupkg-location`, then to `PATH`.
   A decoy `gupkg.cmd` or `gupkg.exe` in the current directory is never run. `gupkg uninstall gupkg` removes `gupkg-location` (unless you
   edited it); a launcher with no bundled copy then prints `Skipped ...gupkg-location: the file does not exist.`, uses a gupkg on `PATH`, and
   with none prints the bootstrap URL and exits 9009. `gupkg cleanup --bundled` removes no bundled copy while no `gupkg-location` names a
   gupkg and none is on `PATH` (and says why), and removes them once gupkg is installed again. Why manual: real launchers on a real installed gupkg.6. **Self-update.** From an installed gupkg run `gupkg self check-updates` then `gupkg self update` against a real second release (build
   one with a bumped version and a `RELEASES.md` row, or use the published one): expect a new `<root>\gupkg\<new>` with a verified `App`
   and `gupkg\` tree, `current` switched, gupkg-location byte-identical. Why manual: needs two real releases on GitHub.
7. **Mark of the Web, SmartScreen and Smart App Control.** On a clean machine download a distribution ZIP with Edge, extract it with
   Explorer, double-click `install.cmd` (expect the Run/Cancel security warning, then the install) and check that the copies under the
   root have no `Zone.Identifier` stream (`Get-Item -Stream * <file>`) and that the receipt (`<package>\.gupkg\receipts\<version>.toml`)
   records `source_marked = true`. Repeat with Smart App Control in evaluation mode: record whether the bundled `python.exe` from the
   extracted ZIP and the installed copy run, and fix `_docs/troubleshooting.md` with what you see. Why manual: needs a real browser download
   and a machine with Smart App Control (spike S2; it could not be run during the rehaul, so it stays owed before 1.0 is published).
8. **System install of a distribution.** In an elevated shell with the system gupkg installed, run
   `gupkg install <folder in Downloads>`: the folder is copied into `C:\opt\<name>\<v>` and installed from there, nothing
   starts from `Downloads`. Why manual: real elevation and a real `C:\opt`.
9. **The registry after the launcher change.** In the registry checkout run `python -m gupkg_registry launchers --registry .`, review the
   diff (every tracker gets `check-updates.cmd`, every pinned version gets the new `install.cmd` and `gupkg-tui.cmd`, `_registry.toml`
   gets the new fingerprints), run `validate`, then commit. Why manual: the registry is a separate repository.

## Portable packages and the modes a package declares (issue 035, task T02)

1. **`035-T02-1` A portable package in the user root.** In a sandbox or a throw-away user root, install a portable package
   (`modes = ["portable"]`, `[portable] persist = ["config"]`) as a normal user, change a file in `App\config`, then publish and
   install a newer version. Expect `<package>\data\config` to hold the change and the new `App\config` to be a junction to it, the
   package to sit in the user root, and its shortcuts and shims to be the normal user ones. Why manual: needs a real NTFS junction and a
   real application that writes its own settings.
2. **`035-T02-2` The refusal texts on a real machine.** From a normal shell, install a system-only package: expect exit 2 and the
   message that names the Start Menu entry "gupkg manager (administrator)" and the administrator-shell command. From an
   administrator shell, install a user-only package and a portable package: expect exit 2 and "Run gupkg without administrator
   rights", with nothing created. Why manual: needs a real elevated token.

## One scope per run: the mode comes from the token (issue 035, task T05)

1. **`035-T05-1` The mode is the token's.** From a normal shell run `gupkg install <package>`, then from an administrator shell run
   `gupkg install <other package>`. Expect the Resolve line `user root %USERPROFILE%\opt (built-in default)` (or the source your
   settings give) in the first and `system root C:\opt (the installed system gupkg)` in the second, the first package under
   `%USERPROFILE%\opt` and the second under `C:\opt`, and no question and no UAC prompt in either. Then run
   `gupkg install <package> --mode user` and `gupkg install system:<package>` from both shells: expect exit code 2 with a usage message and
   nothing changed. Why manual: needs real tokens and real roots.
2. **`035-T05-2` An elevated run from a user-writable gupkg.** From an administrator shell run a gupkg that is installed in a
   user-writable root (for example the bundled copy of a distribution in `%USERPROFILE%\Downloads`): any command, even `gupkg list`,
   ends at once with exit code 2 and "an elevated gupkg must be installed in an admin-only root; run the system gupkg", and
   nothing is read from or written to `C:\opt`. Why manual: needs the real access-control lists of a user profile.
3. **`035-T05-3` `--root` and the system root.** From an administrator shell run `gupkg --root %USERPROFILE%\somewhere install
   <package>`: expect exit code 2 (a user-writable folder cannot be the system root). From a normal shell the same command
   installs under `%USERPROFILE%\somewhere` and the Resolve line says `(--root)`. Why manual: needs the real admin-only judgement.
4. **`035-T05-4` `gupkg self update` from a system gupkg in a normal shell.** With gupkg installed in `C:\opt`, run
   `gupkg self update` from a normal shell: expect exit code 2 and "This gupkg is installed in C:\opt, but the user root of this run
   is ...; run it from an administrator shell". The bootstrap script (`gupkg-bootstrap.cmd`) no longer passes `--mode` to
   `install.cmd`; confirm that `-Scope user` from a normal shell and `-Scope system` from an administrator shell still install. Why
   manual: needs real tokens.
5. **`035-T06-1` An old folder without an install record.** In the real user root, `gupkg list` shows a package folder made by an
   older gupkg (a `current` junction, no `.gupkg\package.toml`) as `broken`; `gupkg list --details` gives the reason `no install
   record (made by an older gupkg); reinstall with: gupkg install <name>`. Run that command: the package is `installed`
   again. Nothing in the folder changed before you ran it. Why manual: needs a real 0.17-layout folder.
6. **`035-T06-2` One root, one registry cache.** From a normal shell with a user root whose registry was never synchronised
   (`gupkg registry status`), `gupkg list --available` lists nothing, even though `C:\opt\.gupkg\registry` holds a cache; after
   `gupkg registry sync` it lists the packages. From an administrator shell `gupkg list --available` marks a package that is
   `["user"]` or portable as `user only` with the reason, and from a normal shell a `["system"]` package as `system only`. A path
   into the other root (`gupkg info C:\opt\<name>` from a normal shell) says `... is not inside this scope's root (...)`. Why manual:
   needs both real roots.

## No elevated relaunch (issue 035, task T07)

1. **`035-T07-1` Nothing starts an elevated process.** From a normal shell run `gupkg install <system-only package>`: expect exit code 2 and
   the message that names the Start Menu entry "gupkg manager (administrator)" and the administrator-shell command, **no UAC prompt**
   and no new window. Then start the Start Menu entry "gupkg manager (administrator)" (if the system gupkg is installed), accept UAC and
   confirm the manager runs in system mode. Why manual: needs a real UAC prompt and a real process list.
2. **`035-T07-2` The stop marker still stops a run.** From an administrator shell run `gupkg update <a> <b> <c> --yes --events-file run.jsonl`;
   read `stop_file` from the first line of `C:\opt\.gupkg\logs\run.jsonl` and create that file while the first package runs: the run
   finishes that package, skips the rest and exits with code 5. Why manual: needs real packages and timing.

## A "run as administrator" shortcut (issue 035, task T08)

1. **`035-T08-1` The administrator shortcut asks for UAC.** On a VM or test root with gupkg installed in system mode, open the Start Menu
   folder `opt` and double-click "gupkg manager (administrator)": expect the UAC prompt, then the manager with the title
   `system · <root>` (after T09). In the link's properties (Advanced), "Run as administrator" is ticked. A user-mode install has only
   "gupkg manager". Why manual: needs a real UAC prompt and a real Start Menu.

## The manager has one scope per run (issue 035, task T09)

1. **`035-T09-1` The manager says which scope it manages.** In a normal terminal start `gupkg-manager`: the terminal window title reads
   `gupkg-manager - user · <root>`, the status line starts with `user` and ends with `root <root>`, and the table has no Mode column. Start it from the
   Start Menu entry "gupkg manager (administrator)" (accept UAC): the title reads `system · <root>`, the status line starts with `system`, and only the
   system packages are listed. Press `a`: a package that the mode cannot install shows `available (system only)` or `(user only)` and, with the cursor on
   it, the status line gives the CLI's reason. Why manual: needs a real terminal title and a real UAC prompt.

## The manager reads in-process and shows what it cannot read (issue 035, task T10)

1. **`035-T10-1` A read that fails is visible.** In a normal terminal start `gupkg-manager`. Make the registry cache unreadable (rename `<root>\.gupkg\registry\main\state.toml` to a folder, or lock it), press `r`: the status line shows `ERROR: registry unreadable: ...` (or `package list unreadable`) and a red notification gives the full text; the list stays usable. Restore the file and press `r`: the error goes away. Why manual: needs a real root and a real file lock.

## Launchers: bundled, then gupkg-location, then PATH (issue 035, task T11)

1. **`035-T11-1` The elevated launcher tests and the elevated PATH step.** From an elevated PowerShell run `scripts\test.cmd tests\test_package_launcher.py -v`: the
   tests marked `needs_admin` (they skip in a standard shell) must pass, in particular `test_an_elevated_launcher_skips_a_user_writable_path_entry_and_names_it`
   (a `gupkg.cmd` in a user-writable `PATH` folder is skipped with the line `Skipped PATH entry <folder>` and the next admin-only entry runs) and
   `test_an_elevated_launcher_refuses_a_gupkg_exe_shim_whose_config_a_user_can_write`. Then, by hand with a real system install in `C:\opt`: from an
   administrator shell run `C:\opt\<package>\<version>\install.cmd` of a registry package that has no bundled copy: it runs the gupkg named by
   `C:\opt\.gupkg\gupkg-location`; grant `Users` write on `C:\opt\.gupkg\gupkg-location` (`icacls`) and run it again: it refuses (`Refusing to run
   elevated`). Put a `gupkg.exe` shim of a user-mode install first on the administrator's `PATH` and delete `gupkg-location`: the line `Skipped PATH
   entry ...` names that folder and the launcher exits 9009 or uses a later admin-only entry. Also, in an admin-only root: (a) a `gupkg.exe` shim and config that are admin-only but sit in a folder `Users` can write (grant `Users` modify on the folder only) is refused, i.e. the entry is skipped with `Skipped PATH entry <folder>`; (b) a `gupkg-location` that names a tree whose package or root folder (the parent two or three levels above `App`) is user-writable under an admin-only launcher root is refused (`Refusing to run elevated`). Why manual: needs a real elevated token and real ACLs (the automated tests see the spec the launcher hands to the check, and the checker itself in a normal shell, but cannot judge real elevated ACLs).
## A package cannot take over another package's names (task T14, not part of issue 035)

1. **`035-T14-1` The second package with the same shim is refused and the root is unchanged.** On a test root (`--root <empty folder>`, never `C:\opt`), create two
   local packages `one` and `two`, each with a version directory whose `pkg.toml` declares `[[shim]] name = "tool"` (a `download_url` of a small zip or
   `source.populate = "none"` with a shipped `App`). Run `gupkg --root <test root> install one`, then `gupkg --root <test root> install two`: the second command
   stops with `two cannot be installed: it would provide the shim 'tool.exe' (...), which the installed package 'one' already provides. Uninstall 'one' first, or
   use a package definition with a different name.` and exit code 2; `<root>\two\current` does not exist and `<root>\bin` holds only the files of `one`. Then
   `gupkg --root <test root> uninstall one` and `install two` succeed. Also `install one two` in a fresh root names both packages and installs neither. Why manual:
   shows the real message and the real folders once.
