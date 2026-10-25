# Issue 033: Bundled mode, package launchers, and bootstrap

> **Status (2026-10-24):** Implemented. Merged in d48404a. Manual checks still owed: tests/manual_smoke.md, 033-1 to 033-9 (elevated launcher refusals, a bundled install leaves no gupkg behind, bootstrap user and system, a squatted C:\opt, an installed gupkg takes over, self-update across two releases, Mark of the Web and Smart App Control (not verifiable on the owner's machine), system install of a distribution, the registry after the launcher change).

Date: 2026-10-22
Priority: High
Epic: 020. Design: `_docs/rehaul_design.md` §21, §9.6 (threat model T1), §9.7 (files only in roots); decisions D14–D20, D26, D27, D29, D30, D32–D34, D40, D43

## Objective

**Bundled mode installs a package without installing extra
infrastructure** (owner's definition, 2026-10-22).

- A program's distribution carries gupkg inside its version directory
  (`<version>\gupkg\`). Someone unzips it and runs `install.cmd`.
- The program is installed with its shortcuts, shims, environment and PATH.
- **gupkg itself is not installed:** no gupkg package, no root forwarder,
  no gupkg shims, and no gupkg `PATH` entry.
- The installed program keeps its bundled copy, so it can be updated,
  repaired and uninstalled later in the same way.

gupkg's own package uses the same mechanism. Its distribution always carries
a bundled gupkg. Installing the package runs its **custom populate script**,
which **copies that tree into `App`**, from which the installed gupkg runs
(D27). Installing it also creates the root data directory `<root>\.gupkg\`,
which holds gupkg's settings, logs and the **root forwarder**. Package
launchers find an installed gupkg only through that forwarder:
`%~dp0..\..\.gupkg\gupkg.cmd`, at a fixed position relative to themselves.

## Current state

- **Launcher order.** `src\package-tui.cmd`, shipped to every registry
  package as `gupkg-tui.cmd`, prefers a gupkg beside the file. It then
  searches **`PATH`** for `gupkg.exe`.
- **gupkg's own package is a special case.** Its tree lives at the version
  root, outside `App`. `src\pkg.local\unpack_app.py` writes an
  `App\RELEASE.txt` marker only so that the engine counts the version as
  populated.
- **Bootstrap logic cannot be reused.** `gupkg-bootstrap.ps1` contains its
  own install logic (place, `gupkg install`, `manager init`), which other
  distributions cannot use.
- **gupkg keeps data outside its roots.** It uses
  `%LOCALAPPDATA%\gupkg\embedded` (removed by issue 016). Issue 013's
  planned `%APPDATA%` config search was never implemented; 0.17 reads
  `gupkg-config.toml` only beside `manager.py`.

## Scope

1. **Reserved names and layout** (§21.2).
   - `gupkg\`, `install.cmd` and `gupkg-tui.cmd` are reserved in version
     directories (D34). Version directories carry no `gupkg-manager.cmd`:
     the general program is reached through the installed gupkg.
   - The engine copies and verifies a bundled tree, and never executes it
     except through the launchers below.
   - A bundled tree carries `VERSION` and `MANIFEST.sha256` (issue 016).
2. **`requires_gupkg` and `[root_forwarder]`** (this issue adds them to the
   issue 023 model). There is no `[source] local` (D27). A distribution
   whose `App` is built from a folder shipped in its version directory uses
   a **custom populate script** in `pkg.local\` that copies the folder into
   `App` (issue 028).
   - The script never moves the folder.
   - Its `App` is as verified as the package itself, and is recoverable
     while the folder exists.
3. **The root data directory and root forwarder** (§9.7, §21.1).
   - **The data directory.** Installing the `gupkg` package creates
     `<root>\.gupkg\`: settings, state, global logs and the registry cache.
     Per-package logs live in `<root>\<name>\.gupkg\logs\` (D26).
   - **The forwarder.** Through the `[root_forwarder]` effect it also writes
     `gupkg.cmd` and `gupkg-manager.cmd` there, forwarding to
     `$PackageRoot\current\App\…`. The effect is recorded in the install
     record and removed by uninstall.
   - **What a forwarder may point to.** A system root's forwarder points
     only to the gupkg in that root. A user root's forwarder may point to a
     user-mode gupkg or to the system gupkg.
4. **Bundled installs** (§21.4).
   - **No forwarder.** They never write one.
   - **The bundled copy stays** in the installed version directory.
   - **It is carried forward** (D32). When `check-updates` or `update`
     produces a new version directory for a package that has a bundled
     copy, the package's own bundled copy is copied into it, whichever
     gupkg is running.
   - **Old version directories keep their bundled copies.** Only an
     explicit `cleanup --bundled` removes them (item 12).
   - **Logs.** A bundled gupkg logs runs that involve no package into its
     own package's `.gupkg\logs\` (D26).
   - **Uninstall.** `uninstall --purge` leaves only the root's
     `.gupkg\logs`.
5. **The package launcher template**
   (`src\gupkg\launchers\package-launcher.cmd`, from which `install.cmd` and
   `gupkg-tui.cmd` are generated). It is plain `cmd`, kept minimal
   (§21.3). `gupkg-tui.cmd` runs `gupkg tui "%~dp0."` through the gupkg it
   finds, which forwards to `gupkg-tui` (issue 030).
   - **Which gupkg runs** (D32):
     1. `%~dp0..\..\.gupkg\gupkg.cmd`, the installed gupkg reachable
        through the root forwarder, unless the launcher is given
        `--no-system-gupkg=true`;
     2. otherwise `%~dp0gupkg\gupkg.cmd`, the package's bundled copy;
     3. if neither exists, print the bootstrap URL and exit 9009.

     There is no version comparison between the two.
   - **Elevated runs check everything they will run, recursively** (D30),
     before starting anything: the root `%~dp0..\..`, the package directory,
     the version directory `%~dp0`, `<root>\.gupkg\` and the forwarder, and
     the whole gupkg and Python tree that will start. For each object the
     owner must be Administrators, SYSTEM or TrustedInstaller, and no other
     security identifier may have a write-type right.
     - **Launched through `current`**, the check follows the junction,
       requires its target to be a version directory of the same package,
       and checks both the junction and its target.
     - The check runs
       `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe
       -NoProfile -NonInteractive` with a fixed script that compares
       security identifiers, never localized names. `%SystemRoot%` is
       trusted to locate PowerShell: malware running as the same account
       is out of scope (T1 rule 5, §9.6).
     - It **fails closed**, and the launcher **refuses** on failure.
     - After the check, it uses the same selection as a non-elevated run.
   - **Never searched:** `PATH`, the registry, AppData, ProgramData or the
     current directory.
   - **Exit 6 fallback:** print a notice and re-run with the bundled copy.
     When elevated, only if that copy passed the check.
   - `GUPKG_NO_PAUSE` keeps today's meaning.
6. **Exit code 6** in the CLI (issue 029). It is raised before any work when
   the target's `format` is unknown, or its `requires_gupkg` is not met.
7. **Installing from a distribution folder** (§21.4).
   - A folder outside every root is copied whole into
     `<mode root>\<name>\<version>\`, then installed.
   - **The copy removes the Mark of the Web** from the copied files, and the
     receipt records that the source was marked (D40).
   - The copy never overwrites; a differing existing copy is `diverged`.
   - `--in-place` installs it where it is, as an ad-hoc root. `--mode
     portable` on a folder already shaped `<name>\<version>\pkg.toml`
     installs it in place (D42, issue 025).
   - **Origin** (D29, issue 028). Such an install is labelled *origin not
     verified*, unless the synced registry publishes a matching fingerprint
     for that package and version; a mismatching fingerprint refuses the
     install.
   - **System mode** is done only by the installed system gupkg from an
     admin shell: `gupkg install <folder> --mode system`. It reads and
     copies the folder, then installs from the admin-only copy. It never
     runs anything from the source.
8. **gupkg's own package in format 2** (§21.6).
   - **Tracker:** no declarative tracker. `_check_updates` has a custom
     `track` script (Reading of D27).
   - **Source:** no `download_url`. A custom populate script in
     `pkg.local\` copies the bundled `gupkg\` folder into `App`. When a
     version written by `check-updates` has no bundled folder yet, the
     script fetches that release itself (D28 allows it) and verifies it
     against `RELEASES.md` and `MANIFEST.sha256`.
   - **Integration:** shims into `$App`, and `[root_forwarder]`.
   - **Commands:** `self check-updates` and `self update` alias
     `check-updates gupkg` and `update gupkg`.
   - **Deleted:** `src\pkg.local\check_update.py`, `unpack_app.py` and the
     `RELEASE.txt` marker. The two new scripts replace them.
   - **The bundled folder stays.** It is the populate script's input, so
     `App` stays recoverable while it exists (D27).
9. **Release ZIP layout** (built by issue 016):
   - `gupkg\<version>\{pkg.toml, pkg.local\…, gupkg\…, install.cmd,
     gupkg-tui.cmd}`;
   - `gupkg\_check_updates\{pkg.toml, pkg.local\…}`.

   The ZIP's `pkg.toml` is fully pinned and has no `download_url`. A
   bootstrap install downloads nothing through gupkg. There is no
   `verified-by-bootstrap`: its origin label follows item 7 (D29).
10. **A thin bootstrap script** (§21.5).
    1. Download the release named in `RELEASES.md`, or `-Source`, and verify
       `-Sha256` or the `RELEASES.md` row.
    2. **User mode:** extract to staging and run `install.cmd`.
    3. **System mode:** the script must be run elevated by the
       administrator.
       1. Create `C:\opt` with an admin-only owner and permissions, or
          refuse if an existing `C:\opt` is not admin-only.
       2. Extract straight into `C:\opt\gupkg\<version>\`.
       3. Run that `install.cmd` elevated. Its check passes, and it finds no
          forwarder yet, so the bundled copy installs gupkg.

    Re-running the bootstrap is also the one-time path from 0.18 to 1.0
    (D33, issue 032).
11. **Authoring commands** (issue 029).
    - `gupkg pkg bundle <version-dir>` (there is no lite bundle in 1.0,
      D43)
    - `gupkg pkg export <version-dir> -o X.zip`
12. **`gupkg cleanup --bundled`** (§21.8, D32). It drops bundled copies in
    a root only when an installed gupkg can take over, that is, when the
    root's forwarder points to an installed gupkg. It runs only when asked;
    plain `cleanup` never touches bundles, and `cleanup` never runs
    automatically (D43).
13. **Registry launchers.** `sync-launchers` replaces the 0.17
    `gupkg-tui.cmd` with the two generated launchers, `install.cmd` and
    `gupkg-tui.cmd` (issue 031). Registry packages do not bundle gupkg.

## Acceptance criteria

- [ ] **A bundled install leaves no gupkg behind.** On a clean VM in user
      mode, unzipping a `gupkg pkg export` distribution into Downloads and
      running `install.cmd` installs the program into
      `%USERPROFILE%\opt\<name>\<v>\`.
  - There is **no** `<root>\gupkg\` package, no `.gupkg\gupkg.cmd`
    forwarder, no gupkg shim and no gupkg `PATH` entry.
  - Nothing is created under `%LOCALAPPDATA%`, `%APPDATA%` or
    `%ProgramData%`, apart from the program's own Start Menu entries.
  - Deleting the Downloads folder leaves the program working.
  - The same install run against `FakeSystem` reads no registry value
    other than `PATH` for its own read-modify-write, as its registry access
    log shows (D38).
- [ ] **The bundled install manages itself.** With no installed gupkg in the
      root, `current\gupkg-tui.cmd` runs the bundled copy; a
      `check-updates` then an update through it installs the new version,
      whose directory has a carried-forward copy of the package's own
      `gupkg\`. The old version directory keeps its `gupkg\`, and switching
      back to it leaves its launchers working. `uninstall --purge` leaves
      only `.gupkg\logs`.
- [ ] **Bootstrap.** `gupkg-bootstrap.cmd` in user mode, and the same script
      run elevated in system mode, install the gupkg package at
      `<root>\gupkg\<v>\`.
  - Its `App` matches `MANIFEST.sha256`.
  - `current`, the shims, `PATH`, the Start Menu entry and
    `<root>\.gupkg\` (with the forwarder and settings) exist.
  - In system mode, `C:\opt` and `C:\opt\.gupkg` are admin-only.
  - `gupkg check` reports healthy.
- [ ] **An installed gupkg takes over.** After gupkg is installed into the
      same root, launchers of an earlier bundled install use the forwarder,
      as the installed gupkg's log shows, also when their bundled copy is
      newer. Given `--no-system-gupkg=true`, they use the bundled copy.
- [ ] **Too-old fallback.** When the installed gupkg is older than a
      package's `requires_gupkg`, it exits 6, and the launcher completes the
      run with the bundled copy.
- [ ] **No `PATH` search.** A decoy `gupkg.exe` on `PATH` and in the current
      directory is never run.
- [ ] **Elevated refusal** (D30). An elevated launcher in a user-writable
      folder (Downloads, or a user root) refuses before starting any Python.
      An elevated launcher in `C:\opt\<name>\<v>\` proceeds.
  - Tests grant a user write, one at a time, on the package directory, the
    version directory, `<root>\.gupkg\`, the forwarder file, and one file
    deep inside the gupkg tree and the Python tree; the launcher must then
    refuse.
  - Run as `C:\opt\<name>\current\install.cmd`, it proceeds when `current`
    points to a version directory of the same package, and refuses when
    `current` points anywhere else.
  - A test blocks PowerShell, and the launcher must then refuse
    (fail-closed).
- [ ] **Squatted system root.** A `C:\opt` pre-created by a standard user
      makes the system bootstrap refuse.
- [ ] **Mark of the Web.** Installing a distribution folder that Explorer
      extracted (every file marked) leaves no `Zone.Identifier` on the
      copies in the root, and the receipt records that the source was
      marked (D40). A manual clean-VM run of `install.cmd` from a
      downloaded ZIP is recorded here, and the troubleshooting guide
      documents the SmartScreen / Smart App Control behaviour of that first
      run and the remedy (job 07, spike S2: Explorer extraction marks every
      file, `tar -xf` marks none, and `Unblock-File` on the ZIP before
      extraction clears it).
- [ ] **Self-update.** `gupkg self check-updates` then `gupkg self update`
      moves to a new release. The new `App` is verified, and the forwarder
      (through `current`) needs no change.
- [ ] **Uninstalling gupkg.** `gupkg uninstall gupkg` removes the forwarder,
      and package launchers in that root fall back to their bundled copies
      where they have one, and otherwise print the bootstrap URL and exit
      9009.
- [ ] **System install of a distribution** (D17). From an admin shell, the
      installed system gupkg's `gupkg install <folder in Downloads> --mode
      system` copies the folder into `C:\opt\<name>\<v>\` and installs from
      the copy; the `FakeSystem` process log contains no path under the
      source folder. `--in-place` installs where the folder is, as an ad-hoc
      root.
- [ ] **Recoverable bundled installs.** After deleting every `App`,
      `install --refresh` repopulates a version whose custom populate
      script copies a folder from its version directory, and a bundled
      install, whose `gupkg\` folder stays intact (moved from issue 028).
- [ ] **`cleanup --bundled`.** With no forwarder in the root, it removes no
      bundled copy and says why. With a forwarder to an installed gupkg, it
      removes the bundled copies, and the launchers then run the installed
      gupkg. Plain `cleanup` removes no bundled copy.

## Tests

- **Launcher tests** (`tests/test_package_launcher.py`) run the generated
  `.cmd` in a temporary directory tree that stands in for a root:
  - fakes that record their argv replace the forwarder target and the
    bundled `gupkg.cmd`;
  - exit 6 and `--no-system-gupkg=true` are simulated;
  - elevated cases, including launches through `current`, run on Windows
    CI under `windows_integration`, with permissions set by the test.
- **Engine tests against `FakeSystem`** cover:
  - population by a custom populate script that copies a shipped folder;
  - carrying the package's own bundle forward;
  - writing and removing the root forwarder, and its absence for bundled
    installs;
  - copying in a distribution folder (with Mark of the Web removal and the
    origin label), `--in-place` and `diverged`;
  - `cleanup --bundled`.
- **Manual smoke tests on real Windows** cover UAC, permission inheritance
  from `C:\opt`, Mark of the Web, and the clean-VM bootstrap.

## Left by issue 029

Issue 029 switched the front end to the 1.0 command line and deleted the 0.17 one. These pieces still speak 0.17 and are this
issue's to rewrite (029 changed none of them, except that the bootstrap no longer fails when `manager init` is missing):

- **`gupkg-bootstrap.ps1`.** It runs `gupkg --scope $Scope install $target` (`--scope` is now a usage error: use
  `install <folder> --mode <mode>`, mind that `<folder>` is a distribution folder that gupkg copies into the root) and then
  `gupkg manager init`, which forwards to a `gupkg-manager.cmd` that does not exist (exit 2). 029 only turned a failing
  `manager init` into a warning; the bootstrap's final lines (`gupkg manager registry sync`) and its header text are 0.17 too.
  Also decide where the manager configuration of 0.17 (`gupkg\gupkg-config.toml`) goes: 1.0 has `settings.toml` files only.
- **gupkg's own package** (`src\pkg.toml`, `src\pkg.local\check_update.py`, `src\pkg.local\unpack_app.py`): `src\pkg.toml` is
  format 1 (`[update]`) and its bins and shortcut name the deleted `gupkg\gupkg-tui.cmd` and `manager tui`; `check_update.py`
  and `unpack_app.py` are the 0.17 hooks.
- **`src\gupkg-tui.cmd` and `src\package-tui.cmd`** (the package launcher template, section 21.2): rewrite them for the root
  forwarder, exit code 6 and `--no-system-gupkg=true`.
- **The README section "Installing gupkg"**, which says the bootstrap "runs `gupkg manager init`".
- **`pkg bundle`** (`gupkg pkg bundle`) copies a verified bundled tree into a distribution but writes no launchers, because the
  launcher template is this issue's: add them (design sections 10.2 and 21.2 say `+ launchers`).
- **Where `MANIFEST.sha256` sits in the 1.0 layout.** `gupkg self status` (`selfmgmt/status.py`) looks under
  `<version>\gupkg\`; confirm that against the layout this issue fixes.
- **The self-update security tests that 029 deleted with the 0.17 `self update`** (`tests/test_self_release_update.py`,
  `tests/test_update_hooks.py`) come back against the new hook, each as its own test:
  - the release ZIP's digest is checked against the `RELEASES.md` row before anything is unpacked;
  - a row without a digest is refused (and `check_update` rejects such a row);
  - the unpacked tree matches its own `MANIFEST.sha256` exactly (no modified, missing or extra file);
  - `RELEASE.txt` names the commit the row lists;
  - the declared version equals the candidate's version;
  - an update to a 1.x release installs nothing and names the bootstrap (the 1.x guard);
  - nothing of the old runtime is carried over, and on any failure the active version is unchanged;
  - `check_update` returns the asset URL and digest of the newest row, and asks for a newer gupkg when it cannot fetch text;
  - a hook that imports an unavailable module fails the update naming the module.
