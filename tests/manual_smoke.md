# Manual smoke checklist

The supported executable boundary is `gupkg.cli:main`, available as `gupkg` or
the source-checkout wrapper (`scripts\run.cmd`). Implementation domains live
directly under `gupkg/`, covering shared
utilities, Windows integration, package layout, configuration, metadata
editing, components, origins, and update staging.

Suggested manual checks on a Windows machine:

1. Run `gupkg --help`.
2. Run `gupkg --version`.
3. Run `gupkg config-fix <version-dir>` against a package without
   `pkg.toml` and confirm a documented starter config with commented examples
   is created.
4. Run `gupkg install <version-dir>` against a package with shortcuts,
   environment variables, PATH entries, and wrapper files.
5. Confirm the package `current` junction points to the expected version.
6. Confirm shortcuts and environment changes land in the selected scope.

Manager release checks:

1. Create the documented `gupkg-config.toml`; run user-only and system-only
   `list`, including a missing root, and inspect human and `--toml` output.
2. Put one selector in both roots and verify separate scoped rows, then test
   valid, absent, broken, and bootstrap `current` entries.
3. Run `manager doctor`, `manager update --check-only`, and `manager update --download-only`. Open TUI Upgrade
   All and verify planning performs no install, settings are visible, and the
   result scrolls with per-target states.
4. Run a mixed-scope batch with UAC accepted and declined; declined elevation
   must leave user packages unchanged.
5. Make one target fail while another succeeds, repair the failed target, and
   safely rerun. Confirm returning to the browser shows new versions.
6. Repeat in narrow and short terminals; verify human and TOML output remains
   usable without relying on color or fixed widths.

Standalone and registry checks:

1. Build a release ZIP with `scripts\build-standalone.cmd` (network needed).
   Confirm the printed SHA-256 equals the digest in `dist\*.zip.sha256`, a
   source-checkout `python\` directory is not copied, and the ZIP contains the
   embedded runtime with its locked dependencies, `pkg.toml`, native shims,
   `gupkg\RELEASE.txt` and `gupkg\MANIFEST.sha256`.
2. Install the release once with
   `gupkg-bootstrap.cmd -Source <bundle.zip> -Sha256 <digest>` (user scope), then
   repeat the same command. Confirm the second run reuses the folder, neither run
   requires elevation, a new terminal finds `gupkg`, `gupkg manager init` created a
   configuration, and `gupkg manager self status` reports a healthy bundled
   runtime. Repeat with `-Scope system` from an elevated shell, and confirm a
   non-elevated shell is refused with a clear message. The automated tests
   cover only `-SkipInstall`; this step is the only coverage of the live
   junction, shim, and PATH changes.
3. Run `gupkg --scope user manager self repair` after removing or renaming only a
   user-scope shim/configuration file. Confirm repair restores it. Repeat in
   system scope with UAC accepted and declined; a declined prompt must leave
   system files unchanged.
4. Create a schema-v2 manager config with separate package roots, bin roots,
   and an external registry cache. Verify `gupkg manager doctor`, `gupkg manager
   registry status`, and `gupkg manager search --offline` are read-only and do not create
   missing package roots.
5. Run `gupkg manager registry sync` with network available, then run `gupkg manager search`
   and install one known selector. Disconnect the network and verify cached
   `search --offline` still works while a selector absent from the cache fails
   cleanly.
6. Corrupt or make unavailable a temporary registry checkout and retry sync.
   Confirm the last validated active tree remains usable and the failed sync
   does not replace it.
7. Confirm that a v1 manager config is rejected with a migration diagnostic;
   schema migration is an explicit administrative task outside the runtime CLI.

## Progress, scrolling, and saved update status (issue 015)

These steps need a real console because they depend on `\r` handling in
`conhost` and Windows Terminal. Use a package whose update is a download of
several MiB.

1. In a console, run `gupkg update --download-only <package>`. Confirm one
   progress line updates in place and ends at `(100%)`. Repeat with output
   redirected to a file: only the `total size` line and the final `(100%)`
   line are written.
2. Repeat step 1 in both `conhost` and Windows Terminal, with a narrow window,
   and confirm the progress line never wraps onto a second line.
3. In `gupkg tui`, run the same update. Confirm progress changes on the
   `Running: ...` status line and the output view gains one final `(100%)` line.
4. Make output longer than the window (for example a verbose hook). Confirm the
   view follows new lines, scrolling up stops following, scrolling back to the
   bottom resumes it, and PgUp/PgDn/Home/End work without a mouse.
5. In `gupkg manager`, choose `Refresh update status`. Confirm
   `Checking i of N: <target>` and one result line per target appear during the
   check, and Escape stops after the current target. Repeat for `Update all`.
6. Refresh, open a package from the browser, run `Update: check`, and return.
   Confirm statuses are kept, the browser reopens with the same filters and
   highlight, and a package updated meanwhile shows `Current`.
7. Quit and start a new `gupkg manager` session. Confirm the saved statuses
   are shown and the Details screen shows the check time.

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
   `gupkg manager self status` reports a healthy install (the installed `Icons`,
   `Shortcuts` and `App` folders and `gupkg-config.toml` must not be listed as
   extra files). Why manual: it needs the real GitHub asset, the registry
   `PATH` change and a machine with no Python.
2. **Same VM, elevated, `-Scope system`.** From an elevated prompt run
   `gupkg-bootstrap.cmd -Scope system`, open a new elevated terminal, and run
   `gupkg --version` and `gupkg manager self status`. Both succeed, and the
   elevated run reports no untrusted folder. Why manual: it needs a real
   administrator token and the `C:\opt` ACL change.
3. **Self-update.** With release N installed and release N+1 published, run
   `gupkg manager self update`. Confirm the new version folder appears beside the
   old one with its own `gupkg\python\`, `gupkg\MANIFEST.sha256` verifies
   (`gupkg manager self status` is healthy), `current` points at it, and the old
   version folder is untouched. Why manual: it needs two real releases and the
   junction and shim activation.
4. **Edited file.** In the installed version folder change one file (for
   example append a line to `gupkg\README.md`), then run
   `gupkg manager self status`. It exits with status 3 and names that file as
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
   a release ZIP and run `gupkg manager self status`. Confirm `sys.executable`
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

## Format 2, settings layers and the migration tool (issue 023)

The automated tests cover the model, the validator, the settings layers (with the
admin-only check replaced by a stand-in) and the migration tool over the real 0.17
registry manifests. These steps need a real administrator token or a real 0.17 install.

1. **Elevated launchers refuse.** Convert a version directory with
   `gupkg-migrate package <dir>`, open an administrator shell, and run the generated
   `install.cmd` and `gupkg-tui.cmd` in it. Each prints "This launcher does not run
   elevated" and exits 1 without starting any gupkg. Run them from a normal shell and
   confirm they start the root forwarder or the bundled gupkg. Why manual: the
   automated launcher tests run in a normal shell, and elevation needs a real token.
2. **Elevated settings layers.** In an administrator shell, with a `settings.toml` placed
   in `<user root>\.gupkg\` that sets `ui.color = "never"`, run the command that shows the
   settings once issue 029 adds it, and confirm the user layer is ignored. Then point
   `--settings` at a file outside `C:\opt` and confirm it is ignored, and at a file inside
   `C:\opt\.gupkg\` and confirm it is used. Also make `C:\opt\.gupkg` writable by `Users`
   and confirm the elevated run refuses the layer. Why manual: it needs a real
   administrator token and real access-control lists (automation uses a stand-in check).
3. **A real 0.17 `C:\opt` tree.** On a copy of a real 0.17 root, run
   `gupkg-migrate package <root>` with `--check` and then without it, and confirm every
   package and `_bootstrap` converts or is reported with its error. Run
   `gupkg-migrate settings <gupkg-config.toml>` and compare the roots, `bin` folders and
   registry with the old file. Adoption of the installed packages (an install record per
   package, no reinstall) is checked here once issues 024 and 028 land. Why manual: it
   needs a real installed 0.17 root.
## The system boundary and sandbox mode (issue 021)

The contract tests run the real `WindowsSystem` against throwaway locations. These
steps cover what they deliberately leave out: the real registry, Start Menu, a UAC
prompt and the broadcast to running programs.

1. **Sandbox leaves the machine alone.** Note the `PATH` of the User environment
   (`[Environment]::GetEnvironmentVariable('Path','User')`) and the contents of
   `%APPDATA%\Microsoft\Windows\Start Menu\opt`. Run
   `gupkg --sandbox %TEMP%\s --scope user install <fixture package with a shortcut,
   an [[environment]] row and a [[path]] row>`. Confirm the `PATH`, the HKCU
   environment values and the Start Menu folder are unchanged, and that
   `%TEMP%\s\system.json` lists the environment value and `PATH` entry while a
   `.lnk.json` record exists below `%TEMP%\s`. Run the same install a second time and
   confirm `system.json` keeps the first run's log. Repeat with `--scope system` after setting
   `"elevated": true` in `system.json` (the sandbox never reads the real token) and
   confirm the HKLM environment and `%ProgramData%` Start Menu are unchanged too.
   Limits to expect: default package and config roots are not redirected (a package
   given by path is installed where it is), and only `git` (on `PATH`) and the bundled 7-Zip may
   run as child processes. Why manual: it compares against the real registry and Start Menu, which
   the automated tests never read.
2. **Real environment broadcast.** After a normal user-scope install of a package with
   a `[[path]]` row, open a new terminal and confirm the entry is on `PATH`; then run
   `gupkg manager self repair` and confirm no error mentions the broadcast. Why manual:
   `WM_SETTINGCHANGE` reaches every open window, so the tests only exercise it through a
   mocked `user32`.
3. **Elevated relaunch through the launcher.** From a non-elevated shell, start a
   `manager update` that includes a system-scope package and accept UAC. Confirm the
   elevated window runs `gupkg.cmd` from the installed version folder. Decline UAC and
   confirm nothing changes. Why manual: UAC cannot be automated.
4. **Administrator detection.** In an elevated and a non-elevated shell run
   `gupkg manager self status` and `gupkg install <package>` with `--scope auto`; confirm
   the elevated shell selects system scope and the other user scope. Why manual: needs
   real tokens.

## Install modes (issue 025)

The automated tests resolve modes, contexts and the elevation step on a `FakeSystem`. These steps
cover the parts that need a real token, a real UAC prompt, Explorer's marking or a second account.
The engine and the command line that call these functions arrive with issues 028 and 029, so the
steps that need `gupkg install --mode ...` are run once those land.

1. **Real relaunch from the system gupkg.** With gupkg installed in `C:\opt` (admin-only), run
   `gupkg install <package> --mode system` from a normal shell. Confirm the question, accept UAC,
   and confirm the install happened in `C:\opt`. Repeat with `--yes` (no question), with
   `--no-input` (exit code 5, nothing changed) and by declining UAC (exit code 5). Why manual: UAC
   cannot be automated.
2. **A user-writable gupkg never elevates.** From a bundled or user-mode gupkg (for example one
   in `%USERPROFILE%\opt`), run the same command. Confirm it refuses and says that system
   installs need the system gupkg, and that no UAC prompt appears. Why manual: it needs the real
   access-control lists of a user profile and of `C:\opt`.
3. **Elevated shell and the user root.** In an elevated shell run `gupkg install <package>` with
   no `--mode` (it prints `system (auto: elevated)`), then `--mode user`, a path inside
   `%USERPROFILE%\opt` and `user:<name>`. Confirm each of the last three is refused with the
   advice to use a normal shell and that nothing under `%USERPROFILE%\opt` changed. Why manual:
   it needs a real elevated token.
4. **Distribution from an administrator shell.** Extract a distribution ZIP with Explorer (every
   file gets a Mark of the Web), then from an elevated shell run
   `gupkg install <folder>\<name>\<version> --mode system`. Confirm the folder is copied into
   `C:\opt\<name>\<version>`, that `Get-Item -Stream * <file>` shows no `Zone.Identifier` on any
   copy, and that the receipt says the source was marked. Why manual: Explorer does the marking.
5. **Portable in place.** On a folder shaped `<name>\<version>\pkg.toml` that declares portable
   in `modes`, run `gupkg install <folder> --mode portable` as a normal user. Confirm nothing is
   copied, that `<name>\..` becomes the portable root, and, with `integrate = "none"` in that
   root's `.gupkg\settings.toml`, that no Start Menu entry or `HKCU\Environment` change appears and
   `gupkg shell` prints the `PATH` line. Why manual: it compares against the real Start Menu and
   registry.

## Event stream, console and logs (issue 022)

The automated tests render with a recording Rich console and write logs below pytest
temporary directories. These steps cover real consoles, real permissions and a real TUI.

1. **Real terminal rendering.** In Windows Terminal, then in a classic console window
   (`conhost`), run `gupkg --scope user install <package with a download origin>`. Confirm
   coloured phase rules, a ✓ or ✗ with the time, a download bar that disappears and leaves one
   line, and a closing summary that names the logs and is the last thing printed. Run it again
   with `set NO_COLOR=1` and with output redirected (`> out.txt`); `out.txt` has `==>` headers,
   no escape characters and no carriage returns (`findstr /R "\x1b" out.txt` finds nothing).
   Why manual: terminal modes and fonts cannot be asserted offline.
2. **System logs are admin-only.** From an elevated shell run `gupkg --scope system install
   <package>`. In a normal shell confirm `icacls C:\opt\.gupkg\logs` and
   `icacls C:\opt\<name>\.gupkg\logs` show write access only for administrators and SYSTEM,
   and that a standard user can neither create nor modify a file there. Why manual: needs
   a real admin-only root.
3. **A standard user cannot steer an elevated log write.** As a standard user prepare
   `<user folder>\events.jsonl` and a junction `mklink /J <user folder>\link C:\Windows\Temp`.
   From an elevated shell run `gupkg --events-file ..\link\x.jsonl install <package>` and
   `gupkg --events-file C:\Windows\x.jsonl install <package>`; both are refused (exit 2) and
   nothing is created outside the log directories. Why manual: UAC cannot be automated.
4. **Nothing behind the TUI.** Open `gupkg tui` on a package whose `[origin]` is an
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
3. **Adoption on a copy of a real 0.17 root.** Copy a real `C:\opt` of a 0.17 machine to a
   throwaway folder, point a settings file at it, run `gupkg-migrate package <copy>`, then
   `gupkg list` (unchanged), `check` (healthy; shows what was adopted) and `uninstall` of one package:
   the 0.17 shortcut and wrapper go away. Why manual: needs real 0.17 data.
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
   portable-capable ones (see `PORTABLE-UNVERIFIED.md`), once with `--mode portable`. Expect exit 0, `App` filled, and for
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
   --mode user` with the registry synced: the install is labelled *verified against registry <name>*. Change one byte of the
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
2. **The real UAC relaunch (needs the system gupkg).** From a normal shell with the system gupkg on `PATH`, run
   `gupkg install system:<name> --yes --events-file run.jsonl --json`. Expect exactly one UAC prompt, an elevated window that runs
   the install and waits for a key, the calling shell waits for it and ends with the child's exit code (`echo %ERRORLEVEL%`),
   and `C:\opt\.gupkg\logs\run.jsonl` (and `C:\opt\<name>\.gupkg\logs\run.jsonl`) that a normal
   shell can read and that ends with `run_end` carrying the result document. Read `stop_file` from the first line, create that
   file during a run of three packages: the elevated run stops after the package it is working on and exits 5. Why manual: needs
   UAC and the real admin-only `C:\opt`.
3. **A user-writable gupkg never elevates.** Run `gupkg install system:<name>` with a user-mode gupkg (under
   `%USERPROFILE%\opt`): expect exit 2 and "System installs need the system gupkg". Why manual: the real ACL check.
4. **The elevated child refuses a user folder.** From an elevated prompt run the system gupkg with `--relaunched`:
   `gupkg install %USERPROFILE%\Downloads\<dist>\<name>\<version> --mode system --relaunched --yes` and expect exit 2 ("accepts
   only package names and paths inside the system root"); without `--relaunched` the same folder is copied into `C:\opt`. Why
   manual: the real elevated token and ACLs.
5. **The forwarders.** `gupkg migrate --help` prints the migration tool's help and `gupkg migrate package <old>` converts a copy of a
   real 0.17 package; `gupkg tui <folder>` opens the package view of that folder and `gupkg manager` opens the browser (both from a normal
   shell; from an elevated shell they refuse with exit 2). Check that the exit code of each program is the exit code of `gupkg`. Why
   manual: needs the real bundle layout and a real console.
6. **`gupkg logs`.** `gupkg logs <name> --open` opens the newest log in the program Windows associates with `.log`; `--path` prints
   its path. Why manual: needs the Windows shell association.
7. **`gupkg self status`.** On an installed gupkg expect `ok` and the bundled interpreter; edit one file below the version
   directory and expect exit 3 naming it. Why manual: needs a real install.
8. **`gupkg pkg check --install` with real downloads.** On a package from the registry (a version directory with its pinned hash)
   expect exit 0 with the real network, and the real user root and registry unchanged. Why manual: real downloads.
9. **`gupkg shell`.** With `[integration.portable] integrate = "none"` and a portable root set, `for /f "delims=" %i in ('gupkg
   shell --mode portable') do %i` puts `<root>\bin` on that console's `PATH`. Why manual: `cmd` interprets the line.
10. **`uninstall --purge` on a real machine.** After a purge the package folder is gone including this run's own package log (it
    cannot stay open), and the root's run log under `<root>\.gupkg\logs` records the deletion. Why manual: only real file locking
    shows whether the log files were released.
11. **A hostile target under real UAC.** With the system gupkg, make a folder `C:\opt\a&calc` (or any system package whose name
    holds `&`, `%`, `!` or `^`) and run `gupkg install "system:a&calc" --yes` from a normal shell: expect exit 2, "not allowed in a
    command that runs with administrator rights", **no UAC prompt** and no Calculator. Then, from an elevated prompt, run
    `gupkg install "system:a&calc" --relaunched --yes`: expect exit 2 ("the elevated child refuses"). Finally run an ordinary
    `gupkg install system:<name> --yes` and check in Task Manager's details tab (command line column) that the elevated process
    is the bundled `python.exe -I -s -B -S ...\bootstrap.py ...` and that no `cmd.exe` was started for it. Why manual: needs the
    real UAC prompt, the real elevated token and a real process list.
12. **The parent waits for the elevated child.** From a normal shell run `gupkg install system:<name> --yes` (a package that
    installs) and then one that fails (for example a corrupt `pkg.toml` in `C:\opt`): the command line stays busy until the
    elevated window closes, and `%ERRORLEVEL%` is 0 and then the failing code. Press Ctrl-C during a long install: the stop marker
    is created, the child finishes its package and the call ends with exit code 5 (or 5 after 30 seconds when the child does not
    end). Decline the UAC prompt: exit code 5. Why manual: needs UAC and a real second process.
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
