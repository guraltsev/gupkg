# Issue 028: Execution engine — phases, receipts, uninstall, cleanup, doctor

Date: 2026-10-22
Priority: Critical
Epic: 020. Design: `_docs/rehaul_design.md` §7.4, §13

## Problem

- **Install and update are two separate pipelines.**
  - `gupkg.install_package` mixes resolution, scope, bootstrap promotion,
    junction handling, origin population and components.
  - The update path (`_run_update`, `prepare_update`, `os.replace`, receipt,
    `install_downloaded_update`) is a second pipeline. It picks receipts by
    mtime and calls back into `install_package`.
- **`current` is linked before `App` is populated** (`gupkg.py:208-240`). A
  failed population leaves `current` pointing at an empty version.
- **New versions are copied from the *active* version's tree**, not from the
  tracker or the registry. Edits to a template never propagate.
- **Removed shortcuts and shims are never cleaned up.**
- **Lifecycle commands are missing:** there is no uninstall, no
  pruning of versions, receipts or work directories, and no rollback command
  other than `--allow-downgrade`.
- **Doctor checks manifests only.** It does not check `App`, shortcuts,
  shims or PATH.

## Scope

1. **`engine/planner.py`.** A pure function from a request (install, update,
   switch, uninstall, cleanup, repair), a `PackageView` (issue 024), a
   `ModeContext` (issue 025) and the settings to an `InstallPlan`. The plan
   contains the phases of §13.1 and the declared `Effect`s.
2. **`engine/executor.py`.** It runs a plan through `System` (issue 021),
   emits events (issue 022), and enforces the following:
   - population is atomic: `App` is staged in `.gupkg\work` and renamed;
   - `current` is switched **after** population and the portable data
     hand-over;
   - integration diffs the desired effects against the install record.
     Effects that are new or changed are applied; stale ones are removed.
     The environment change is broadcast once;
   - the receipt and install record are written last, and the receipt
     includes the log path;
   - a failure before Activate leaves no trace outside `.gupkg\work`.
3. **Check is a separate plan** (§7.4, §13.1; owner's decision D11). Its
   steps are:
   1. run the tracker;
   2. let the catalog compare the result with the known versions;
   3. `render_pinned`;
   4. write `<package>\<version>\` atomically (user and portable roots), or a
      proposal (system roots, §9.3);
   5. **stop**.

   **No install plan contains a tracker step.** `install` and `update` choose
   from the catalog's known candidates only. An elevated `update` admits
   pending system proposals: it shows each one, validates it, and builds the
   version directory with code taken only from the admin-only
   `_check_updates`. Only then does it install.

   **The network boundary (§9.3, decision D10):**
   - **Update sources.** New versions come only from `_check_updates`,
     through `check`, and from registry pinned versions. They are never derived from the installed
     version's own `pkg.toml`; today's `prepare_update` copies `[update]`
     forward, and that path is removed.
   - **Checks run unelevated.** `check` always runs with user privileges,
     even for a system-mode package. An elevated `gupkg check` drops
     elevation, or refuses with a hint.
   - **Fixed downloads.** Installing a version directory downloads only the
     URLs written in its `pkg.toml`.
4. **`engine/lifecycle.py`.** The public entry points `check`, `install`,
   `update` (`download_only` or full), `switch`, `uninstall [purge]`,
   `cleanup [keep]`, `doctor [fix]` and `repair`.
   - Each returns a typed `Result`. Rendering is left to the front ends.
5. **Several targets in one CLI call** (§13.2):
   - the targets are resolved once and elevation is checked once;
   - the run continues after a failure;
   - every package's output is logged;
   - cancellation takes effect between packages and between phases.

   Planning, confirmation and grouping by mode belong to `gupkg-manager`
   (issue 030). The engine provides the `--plan` data for them.
6. **Doctor** reports, for each package, whether `current` is valid and its
   `App` is non-empty, and whether every effect in the install record is
   present. Missing shortcuts, shims, environment values and PATH entries are
   reported as drift. It also reports shadowing across scopes and unverified
   installs. `--fix` re-applies the integration.
7. **Adopting 0.17 installs.** The first run over an installed 0.17 package
   writes an install record from its declared components and marks them
   `adopted`, so uninstall and stale-effect cleanup work without a
   reinstall.
8. **Old code is deleted.** `gupkg.py`'s install and update coordinators,
   `updates.prepare_update` and its receipt-by-mtime logic,
   `origin.populate_*` and the `manager.py` executor are removed. Their
   behaviour is now covered by the engine.

## Acceptance criteria

- [ ] On `FakeSystem`, each of these operations has an end-to-end test of
      the resulting machine state:
  - install
  - install again (a repair; nothing is downloaded)
  - `check`, which writes a version directory and installs nothing
  - a following `update`, which installs it
  - update via the registry
  - switch back
  - uninstall
  - uninstall with `--purge`
  - cleanup
- [ ] When a package version drops a `[[shortcut]]` and a `[[shim]]`,
      updating to it removes both from the system.
- [ ] Installing a version directory fetches only the URLs written in its
      `pkg.toml`. This is checked with the `FakeSystem` fetch log, which must
      contain no other URL.
- [ ] `check` on a system package, run unelevated, writes only a proposal.
      A following elevated `update` admits it (taking hooks from the
      admin-only `_check_updates`, never from the proposal) and installs it.
- [ ] `install` and `update` on a package with only `_check_updates` make no
      tracker request. The `FakeSystem` fetch log is empty.
- [ ] **A recoverable `App` is disposable** (design §4). For every fixture
      version that has a `download_url`, whatever its `shipped` value, the
      following leaves the package `installed` and healthy, with
      integration unchanged:
      1. delete the `App` of every version, including the current one;
      2. run `install --refresh` (or `doctor --fix`).

      The same check for `local` and for bundled installs is an acceptance
      criterion of issue 033.
- [ ] **A shipped `App` is never lost.** For a version whose `[source]` has
      `[source] shipped = true`, explicit or by default when there is no
      `download_url`:
  - `cleanup` and plain `uninstall` keep its `App`;
  - `uninstall --purge` asks for an explicit confirmation (with `--no-input`
    it exits 5 and deletes nothing), which names the `App` as unrecoverable
    when there is no `download_url`;
  - `doctor` reports a modified file when the version carries an `App`
    manifest.
  - With a `download_url` as well, the shipped `App` is still never dropped
    or replaced by `cleanup` or `install --refresh`. After the `App` is
    deleted by hand, repair recovers it from the `download_url`. Without a
    `download_url`, repair fails with `App is missing and no way to recover
    it.`
- [ ] When population fails after a download, `current` still points at the
      previous version, and the system state has not changed.
- [ ] `doctor` reports a deleted shim and a missing PATH entry, and
      `doctor --fix` restores both.
- [ ] An installed 0.17 package is adopted: after the first 1.0 run,
      `uninstall` removes its shortcuts, wrappers and PATH entry.
- [ ] A 3-package batch where package 2 fails still installs package 3, and
      both the summary and the log name the failure.
- [ ] **Install modes end to end** (moved from issue 025):
  - a portable fixture app that writes `settings.ini` and `Data\` into its
    `App` keeps both across `update` and `switch` back and forth;
  - installing a package with `[nonportable] remove = ["portable"]` in user
    mode removes the marker, and in portable mode it creates the
    `[portable] markers` instead;
  - `--mode system` on a package without `system` in `modes` fails before
    any download, and so does portable mode on a package without
    `portable`;
  - with `integrate = "none"`, a portable install makes no HKCU, Start Menu
    or PATH changes.

  Each runs end to end against `FakeSystem`.
