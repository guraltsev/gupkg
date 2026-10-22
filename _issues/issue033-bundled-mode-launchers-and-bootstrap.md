# Issue 033: Bundled mode, package launchers, and bootstrap

Date: 2026-10-22
Priority: High
Epic: 020. Design: `_docs/rehaul_design.md` §21, §9.6 (threat model T1), §9.7 (files only in roots); decisions D14–D20

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
a bundled gupkg. Installing the package **copies that tree into `App`**,
from which the installed gupkg runs. Installing it also creates the root
data directory `<root>\.gupkg\`, which holds gupkg's settings, logs and the
**root forwarder**. Package launchers find an installed gupkg only through
that forwarder: `%~dp0..\..\.gupkg\gupkg.cmd`, at a fixed position relative
to themselves.

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
  `%LOCALAPPDATA%\gupkg\embedded` (removed by issue 016) (removed by
  issue 016). Issue 013's planned `%APPDATA%` config search was never
  implemented; 0.17 reads `gupkg-config.toml` only beside `manager.py`.

## Scope

1. **Reserved names and layout** (§21.2).
   - `gupkg\`, `install.cmd` and `gupkg-manager.cmd` are reserved in version
     directories.
   - The engine copies and verifies a bundled tree, and never executes it.
   - A bundled tree carries `VERSION` and `MANIFEST.sha256` (issue 016).
2. **`[source] local = "<dir>"`** (this issue adds it, with `requires_gupkg`
   and `[root_forwarder]`, to the issue 023 model and the issue 026 unpack
   services).
   - It populates `App` by **copying** a verified folder shipped in the
     version directory, and falls back to `download_url`.
   - It never moves the folder.
3. **The root data directory and root forwarder** (§9.7, §21.1).
   - **The data directory.** Installing the `gupkg` package creates
     `<root>\.gupkg\`: settings, state, logs, registry cache, and, in user
     roots, proposals.
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
   - **It is carried forward.** When `check` or `update` produces a new
     version directory in a root that has no forwarder, the running bundled
     gupkg is copied into it.
   - **Uninstall.** `uninstall --purge` leaves only the root's
     `.gupkg\logs`.
5. **The package launcher template**
   (`src\gupkg\launchers\package-launcher.cmd`, from which `install.cmd` and
   `gupkg-manager.cmd` are generated). It is plain `cmd`, kept minimal
   (§21.3).
   - **Non-elevated lookup:**
     1. `%~dp0..\..\.gupkg\gupkg.cmd`, the root forwarder;
     2. `%~dp0gupkg\gupkg.cmd`, the bundled copy;
     3. if neither exists, print the bootstrap URL and exit 9009.
   - **Elevated runs:** first verify that the root `%~dp0..\..` and the
     version directory `%~dp0` are not user-writable. The owner must be
     Administrators, SYSTEM or TrustedInstaller, and no other security
     identifier may have a write-type right.
     - The check runs
       `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe
       -NoProfile -NonInteractive` with a fixed `Get-Acl` script that
       compares security identifiers, never localized names.
     - It **fails closed**, and the launcher **refuses** on failure.
     - After the check, it uses the same lookup as a non-elevated run.
   - **Never searched:** `PATH`, the registry, AppData, ProgramData or the
     current directory.
   - **Exit 6 fallback:** print a notice and re-run with the bundled copy.
     When elevated, only if that copy lies in the verified location.
   - `GUPKG_NO_PAUSE` keeps today's meaning.
6. **Exit code 6** in the CLI (issue 029). It is raised before any work when
   the target's `format` is unknown, or its `requires_gupkg` is not met.
7. **Installing from a distribution folder** (§21.4).
   - A folder outside every root is copied whole into
     `<mode root>\<name>\<version>\`, then installed.
   - The copy never overwrites; a differing existing copy is `diverged`.
   - `--in-place` installs it where it is, as an ad-hoc root.
   - **System mode** is done only by the installed system gupkg from an
     admin shell: `gupkg install <folder> --mode system`. It reads and
     copies the folder, then installs from the admin-only copy. It never
     runs anything from the source.
8. **gupkg's own package in format 2** (§21.6).
   - **Tracker:** GitHub provider, with the checksum from `RELEASES.md`.
   - **Source:** `local = "gupkg"`, plus a `download_url` with
     `subdir = "gupkg/${version}/gupkg"`, and `verify_manifest`.
   - **Integration:** shims into `$App`, and `[root_forwarder]`.
   - **Commands:** `self check` and `self update` alias `check gupkg` and
     `update gupkg`.
   - **Deleted:** `src\pkg.local\check_update.py`, `unpack_app.py` and the
     `RELEASE.txt` marker.
   - **Cleanup:** `cleanup` drops the redundant bundled copy once `App` is
     populated.
9. **Release ZIP layout** (built by issue 016):
   - `gupkg\<version>\{pkg.toml, gupkg\…, install.cmd, gupkg-manager.cmd}`;
   - `gupkg\_check_updates\pkg.toml`.

   The ZIP's `pkg.toml` is fully pinned, with a *fixed*
   `[source.checksum]` page match on `RELEASES.md` for that exact version. A
   bootstrap downloads nothing; its receipt records
   `verified-by-bootstrap`.
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
11. **Authoring commands** (issue 029).
    - `gupkg pkg bundle <version-dir> [--lite]`
    - `gupkg pkg export <version-dir> -o X.zip`
12. **`gupkg cleanup --bundled`** (§21.8). It drops bundled copies in a root
    only when the root's forwarder points to an installed gupkg of at least
    that version. Off by default.
13. **Registry launchers.** `sync-launchers` replaces `gupkg-tui.cmd` with the
    two generated launchers (issue 031). Registry packages do not bundle
    gupkg.

## Acceptance criteria

- [ ] **A bundled install leaves no gupkg behind.** On a clean VM in user
      mode, unzipping a `gupkg pkg export` distribution into Downloads and
      running `install.cmd` installs the program into
      `%USERPROFILE%\opt\<name>\<v>\`.
  - There is **no** `<root>\gupkg\` package, no `.gupkg\gupkg.cmd`
    forwarder, no gupkg shim and no gupkg `PATH` entry.
  - Nothing is created under `%LOCALAPPDATA%`, `%APPDATA%` or
    `%ProgramData%`, apart from the program's own Start Menu entries.
  - No registry value is read other than `PATH` for its own
    read-modify-write.
  - Deleting the Downloads folder leaves the program working.
- [ ] **The bundled install manages itself.** With no installed gupkg in the
      root, `current\gupkg-manager.cmd` runs the bundled copy; a check then an
      update through it installs the new version, whose directory has a
      carried-forward `gupkg\`. `uninstall --purge`
      leaves only `.gupkg\logs`.
- [ ] **Bootstrap.** `gupkg-bootstrap.cmd` in user mode, and the same script
      run elevated in system mode, install the gupkg package at
      `<root>\gupkg\<v>\`.
  - Its `App` matches `MANIFEST.sha256`.
  - `current`, the shims, `PATH`, the Start Menu entry and
    `<root>\.gupkg\` (with the forwarder and settings) exist.
  - In system mode, `C:\opt` and `C:\opt\.gupkg` are admin-only.
  - `gupkg doctor` reports healthy.
- [ ] **An installed gupkg takes over.** After gupkg is installed into the
      same root, launchers of an earlier bundled install use the forwarder,
      as the installed gupkg's log shows.
- [ ] **Too-old fallback.** When the installed gupkg is older than a
      package's `requires_gupkg`, it exits 6, and the launcher completes the
      run with the bundled copy.
- [ ] **No `PATH` search.** A decoy `gupkg.exe` on `PATH` and in the current
      directory is never run.
- [ ] **Elevated refusal.** An elevated launcher in a user-writable folder
      (Downloads, or a user root) refuses before starting any Python. An
      elevated launcher in `C:\opt\<name>\<v>\` proceeds.
  - A test grants a user write on the version directory, and the launcher
    must then refuse.
  - A test blocks PowerShell, and the launcher must then refuse
    (fail-closed).
- [ ] **Squatted system root.** A `C:\opt` pre-created by a standard user
      makes the system bootstrap refuse.
- [ ] **Mark of the Web.** A manual clean-VM run of `install.cmd` from a
      downloaded ZIP is recorded here, and the troubleshooting guide
      documents the SmartScreen / Smart App Control behaviour and the remedy
      (job 07, spike S2: Explorer extraction marks every file, `tar -xf`
      marks none, and `Unblock-File` on the ZIP before extraction clears
      it).
- [ ] **Self-update.** `gupkg self check` then `gupkg self update` moves to a
      new release. The new `App` is verified, and the forwarder (through
      `current`) needs no change.
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
      `install --refresh` repopulates a version with `local` and a
      `download_url`, and a bundled install, whose `gupkg\` folder stays
      intact (moved from issue 028).

## Tests

- **Launcher tests** (`tests/test_package_launcher.py`) run the generated
  `.cmd` in a temporary directory tree that stands in for a root:
  - fakes that record their argv replace the forwarder target and the
    bundled `gupkg.cmd`;
  - exit 6 is simulated;
  - elevated cases run on Windows CI under `windows_integration`, with
    permissions set by the test.
- **Engine tests against `FakeSystem`** cover:
  - `local` population and its fallback to `download_url`;
  - carrying a bundle forward;
  - writing and removing the root forwarder, and its absence for bundled
    installs;
  - copying in a distribution folder, `--in-place` and `diverged`;
  - `cleanup --bundled`.
- **Manual smoke tests on real Windows** cover UAC, permission inheritance
  from `C:\opt`, Mark of the Web, and the clean-VM bootstrap.
