# Issue 025: First-class install modes (system, user, portable)

Date: 2026-10-22
Priority: Critical
Epic: 020. Design: `_docs/rehaul_design.md` §6

## Problem

- **Scope is spread over four modules**, each covering part of it:
  - `layout.compute_scope_paths`
  - `manager.InstallationContext` (bin only)
  - `windows.environment_registry_location`
  - `distribution._user_bin` / `_system_bin`, which `self status` uses
    instead of the configured bins
- **`auto` means four different things across commands**:
  - for package install, admin means system;
  - for manager listing, both roots;
  - `manager install` rejects it;
  - `self repair` treats it as user.
- **The Start Menu root cannot be configured.**
- **Scope is not tied to the package.** A package under the user root can be
  installed with `--scope system`. Package mode ignores the manager's `[bin]`,
  so `gupkg install` and `gupkg manager install` put wrappers in different
  places.
- **The CLI does not elevate.** Only the TUI relaunches elevated, so CLI
  system targets fail one by one with "requires administrator".
- **"Portable" has no real meaning today.** `only_portable` only forbids
  system scope. Apps that keep settings inside `App` lose them on every
  update, because every update builds a new `App`. This affects tixati,
  SumatraPDF and, upstream, Notepad++ (`doLocalConf.xml`), HWiNFO,
  CrystalDisk*, Rufus, WinSCP and others. keypirinha, mogan and
  systeminformer delete their portable markers to avoid this.

## Scope

1. **`ModeContext`.** An immutable object resolved once per package
   operation. It holds:
   - mode and root;
   - bin and Start Menu directories;
   - the environment target (HKLM, HKCU or none);
   - whether elevation is required;
   - the log directory and the settings layers.

   Its values come from the settings `[roots]` and `[integration.*]`
   (issue 023). Every subsystem reads only this object. The four sources
   listed above are deleted.
2. **Mode selection** follows §6.2: an explicit `--mode` or qualified
   selector, then the install record, then the containing root, then
   `default_mode`. The default is **`auto`** (owner's decision D4,
   2026-10-22):
   - an elevated process selects `system`, or `user` with a notice when the
     package lacks `system` in `modes`;
   - a non-elevated process selects `user`;
   - `auto` never selects `portable`.

   `auto` is consulted only when nothing earlier decides, and it means the
   same in every command. The chosen mode and the reason for it (for example
   `system (auto: elevated)`) are printed and logged.
3. **Mode support is checked before any work.** `modes = [...]` in
   `pkg.toml` is enforced up front. System mode never allows portable
   packages.
4. **Elevation for system mode** follows threat model T1 (design §9.6,
   D19). It is checked once, up front, for the whole CLI call.
   - **Only an admin-only gupkg elevates, and only by relaunching itself.**
     Before relaunching, gupkg checks that its own version directory and
     root are not user-writable. That check is meaningful because the
     running code is the admin-only copy. A user-mode or bundled gupkg in a
     user-writable location never elevates; it reports that system installs
     need the system gupkg.
   - **The elevated side resolves everything itself.** It accepts only
     package names, or paths inside the admin-only system root, from the
     non-elevated side. It reads only the system root's
     `.gupkg\settings.toml`, and never user settings, user roots, the user's
     environment or the registry (D18).
   - **The CLI** relaunches itself elevated after a confirmation, or
     immediately with `--yes`. A declined prompt returns exit code 5 and
     leaves everything unchanged.
   - **`gupkg-manager`** groups all system targets into one elevated CLI
     call (issue 030).
5. **Portable mode**, as in §6.3:
   - a `[roots].portable` setting, and portable targets at any path
     (`--mode portable PATH`);
   - a `<package>\data` directory;
   - `[portable] persist` (directories are junctioned, files are copied in
     and copied back), `markers`, `args` and `env`; and `[nonportable]
     remove`;
   - first population migrates the vendor's default config into `data\`;
   - on switch or update, persisted files are copied back before `current`
     moves;
   - `integrate = "user" | "none"`; with `none`, shims go to `<root>\bin`,
     and `gupkg shell` prints the PATH line to use;
   - `uninstall` keeps `data\` unless `--purge` is given.
6. **Same-name packages in two scopes** are detected (shared with issue 024)
   and reported as a warning about PATH and Start Menu shadowing.

## Acceptance criteria

- [ ] **T1 relaunch.** A gupkg running from a user-writable location refuses
      to relaunch elevated. An elevated gupkg given
      `--settings <user-writable path>`, or a target outside the system root
      for a system-mode operation, refuses, or treats the target as data to
      copy (design §21.4).

- [ ] `gupkg install X --mode user`, the same command with `--mode system`,
      and the manager-driven equivalents all put shortcuts, shims, the
      environment and PATH in the locations configured in the settings, and
      are identical in the CLI and the UI.
- [ ] With `default_mode = "auto"` and no `--mode`:
  - `gupkg install X` from an elevated shell installs in system mode and
    prints `system (auto: elevated)`;
  - from a normal shell it installs in user mode;
  - an elevated install of a package without `system` in `modes` installs in
    user mode, with a notice.
- [ ] An existing user-mode package, updated from an elevated shell, stays
      in user mode: the install record wins over `auto`.
- [ ] A portable fixture app that writes `settings.ini` and `Data\` into its
      `App` keeps both across `update` and `switch` back and forth. The test
      runs end to end against `FakeSystem`.
- [ ] Installing a package with `[nonportable] remove = ["portable"]` in
      user mode removes the marker. In portable mode, it creates the
      `[portable] markers` instead.
- [ ] `--mode system` on a package without `system` in `modes` fails before
      any download. So does portable mode on a package without `portable`.
- [ ] With `integrate = "none"`, a portable install makes no HKCU, Start Menu
      or PATH changes. The test checks `FakeSystem` state.

## Notes

This issue fixes the shared-state risk of portable installs in a system
root by forbidding them, and documents why in the operations guide.
