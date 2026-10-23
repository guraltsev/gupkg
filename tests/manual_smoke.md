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
   given by path is installed where it is), and only `git` and 7-Zip may run as child
   processes. Why manual: it compares against the real registry and Start Menu, which
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
