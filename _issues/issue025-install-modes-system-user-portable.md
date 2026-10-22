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
   - the log directories (D26): the package's `<root>\<name>\.gupkg\logs\`
     for per-package information, and `<root>\.gupkg\logs\` for global run
     information only;
   - the settings layers.

   Its values come from the settings `[roots]` and `[integration.*]`
   (issue 023). Every subsystem reads only this object. The four sources
   listed above are deleted.
2. **Mode selection** follows §6.2: an explicit `--mode` or qualified
   selector, then the install record, then the containing root, then
   `default_mode`. The default is **`auto`** (owner's decision D4,
   2026-10-22):
   - an elevated process selects `system`; when the package lacks `system`
     in `modes`, it refuses and says to use a normal shell (D31);
   - a non-elevated process selects `user`;
   - `auto` never selects `portable`.

   `auto` is consulted only when nothing earlier decides, and it means the
   same in every command. The chosen mode and the reason for it (for example
   `system (auto: elevated)`) are printed and logged.

   **An elevated gupkg never acts on a user root** (D31): not through the
   `auto` fallback, not through an explicit `--mode user`, and not through
   an install record or a containing root that selects a user root. It
   refuses and says to use a normal shell.
3. **Mode support is checked before any work.** `modes = [...]` in `pkg.toml`
   is enforced up front. A portable root is never the system root or inside it
   (§6.3).
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
     immediately with `--yes` (D25). A declined prompt, or the question
     under `--no-input`, returns exit code 5 and leaves everything
     unchanged.
   - **`gupkg-manager`** groups all system targets into one elevated CLI
     call (issue 030).
5. **Portable mode**, as in §6.3:
   - a `[roots].portable` setting, and portable targets at any path
     (`--mode portable PATH`);
   - **the portable target** (D42): `--mode portable` on a folder already
     shaped `<name>\<version>\pkg.toml` installs it in place, and the
     folder above `<name>` is the portable root. Otherwise the configured
     portable root is used, or gupkg asks for one (an error under
     `--no-input`);
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
      to relaunch elevated. It reports that system installs
      need the system gupkg. An elevated run ignores a `--settings` file
      outside the system root. A relaunched elevated child refuses any target
      that is not a package name or a path inside the system root. From an
      admin shell, `install <folder outside every root> --mode system` copies
      the folder into the system root and installs from the copy (design
      §21.4). The copies carry no Mark of the Web, and when the source was
      marked, the receipt says so (D40).

- [ ] For `user` and `system`, the resolved `ModeContext` takes bin, Start
      Menu, environment target and root from `[integration.*]` and `[roots]`,
      including non-default values.
- [ ] With `default_mode = "auto"` and no `--mode`:
  - an admin-only gupkg in an elevated process (`FakeSystem` elevation set)
    resolves `system`, with the reason `system (auto: elevated)`;
  - a non-elevated process resolves `user`;
  - an elevated process refuses a package without `system` in `modes`, and
    says to run it from a non-elevated shell;
  - `portable` is never resolved by `auto`.
- [ ] In a non-elevated process, an existing user-mode package resolves to
      user mode: the install record wins over `auto`. A path target inside a
      configured root keeps that root's mode.
- [ ] **No elevated user-root actions (D31).** An elevated process refuses,
      and says to use a normal shell, for an existing user-mode package, a
      path target inside a user root, and an explicit `--mode user`. Nothing
      under the user root changes.
- [ ] **Portable target (D42).** `--mode portable` on a folder shaped
      `<name>\<version>\pkg.toml` installs it in place, with the folder above
      `<name>` as the portable root, and copies nothing. On any other target
      it uses `[roots].portable`; with no portable root configured it asks,
      and under `--no-input` it fails without changes.
- [ ] The end-to-end checks of portable persistence, `[nonportable]`,
      unsupported modes and `integrate = "none"` are acceptance criteria of
      issue 028, which provides the engine they need.

## Notes

This issue fixes the shared-state risk of portable installs in a system
root by forbidding them, and documents why in the operations guide.
