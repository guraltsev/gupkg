# Issue 028: Execution engine — phases, receipts, uninstall, cleanup, check

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
- **`doctor` checks manifests only.** It does not check `App`, shortcuts,
  shims or PATH. (`doctor` becomes the health check `gupkg check`, D21.)

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
3. **`check-updates` is a separate plan** (§7.4, §13.1; owner's decisions
   D11, D21–D23). Its steps are:
   1. run the tracker;
   2. let the catalog compare the result with the known versions;
   3. `render_pinned`;
   4. write `<package>\<version>\` atomically, carrying forward the
      package's own bundled gupkg and launchers (issue 033, D32);
   5. download and verify the payload (issue 027) and populate its `App`,
      unless `--no-download` is given;
   6. **stop.** The new version is ready but not active.

   **Who runs it (D22, D31):**
   - **System packages.** An administrator runs `check-updates` from an
     admin shell, and it writes straight into the system root. There are
     no proposals, no inbox and no admission step.
   - **A normal shell** may run it for every package. On a system package
     it implies `--no-download`, so it only reports what it found and
     writes nothing.
   - **An elevated `check-updates` on a user package refuses** (D31) and
     says to use a normal shell.
   - **`--no-download`** writes only the `pkg.toml`-and-support-files
     directory where the root is writable, and only reports otherwise.

   **No install plan contains a tracker step.** `install` and `update` choose
   from the catalog's known candidates only. `update` (or `install
   name@ver`) activates and integrates a version that `check-updates`
   prepared, as a separate operation (D11). It populates a version that has
   no `App` yet.

   **Network rules (§9.3; D10 as reduced by D28):**
   - **Update sources.** New versions come only from `_check_updates`,
     through `check-updates`, and from registry pinned versions. They are
     never derived from the installed version's own `pkg.toml`; today's
     `prepare_update` copies `[update]` forward, and that path is removed.
   - **No update discovery in a version directory.** Validation rejects a
     version directory with a `[track]` table or a `track` hook. Other
     scripts and hooks in a version directory may use the network, through
     the shared HTTP client in their context.
   - **Fixed downloads.** The built-in download script downloads only the
     URLs written in the version's `pkg.toml`.
4. **Populating `App`** (§4; D27). `[source]` names how `App` is populated:
   - **The built-in download script** (`download_url`, optional `sha256`):
     it downloads, verifies (issue 027), unpacks (issue 026), then runs the
     package's remaining local install steps (`[[prepare]]`) through a
     hook.
   - **A custom populate script** in `pkg.local\`: Python, `.ps1`,
     `.cmd`/`.bat` or an executable. A Python script gets the hook context;
     any other kind runs as a subprocess and receives the context as JSON.
     It can run the `[[prepare]]` steps through its context. Its `App` is
     **as verified as the package itself**, and is **recoverable while the
     script's inputs exist**: repair re-runs the script, and `cleanup`
     drops that `App` only then.
   - **`shipped`**: `App` is present and nothing populates it.
   - There is no `[source] local`.
5. **Origin of installs with no download** (D29, D40). A version installed
   from a distribution folder is labelled *origin not verified*, unless the
   synced registry on disk publishes a **fingerprint** for that package and
   version (one SHA-256 over the version directory's files) and it
   matches: then it is labelled *verified against registry `<name>`*. A
   **fingerprint mismatch refuses the install**, like a checksum mismatch.
   Copying a distribution into a root removes the Mark of the Web from the
   copies, and the receipt records that the source was marked.
6. **`App` fingerprint** (D41). Every install records a SHA-256 of every
   file in `App` by default. A package opts out with `record_manifest =
   false`; a global setting switches it off for all packages.
7. **`engine/lifecycle.py`.** The public entry points `check_updates`,
   `install`, `update` (`download_only` or full), `switch`, `uninstall
   [purge]`, `cleanup [keep]`, `check [fix]` and `repair`.
   - Each returns a typed `Result`. Rendering is left to the front ends.
   - **`cleanup` never runs automatically** (D43). Only the manager's
     `[cleanup] auto = true` runs it, after `gupkg-manager update --all`
     (issue 030).
8. **Several targets in one CLI call** (§13.2):
   - the targets are resolved once and elevation is checked once;
   - the run continues after a failure;
   - every package's output is logged;
   - **cancel stops after the current package** (D37): the running CLI
     finishes that package cleanly and skips the rest. An elevated call is
     asked to stop through a stop marker, which can only make it stop.

   Planning, grouping by mode and checksum questions belong to
   `gupkg-manager` (issue 030). The CLI's own confirmations follow D25
   (issue 029). The engine provides the `--plan` data.
9. **The health check `gupkg check [<target>…] [--fix]`** (D21; it replaces
   `doctor`) reports, for each package, whether `current` is valid and its
   `App` is non-empty, and whether every effect in the install record is
   present. Missing shortcuts, shims, environment values and PATH entries
   are reported as drift. It also reports:
   - shadowing across scopes;
   - unverified installs, including skipped checksums (D24) and the origin
     label of no-download installs;
   - a registry fingerprint mismatch for an installed version (D29);
   - files that differ from the recorded `App` fingerprint (D41).

   `--fix` re-applies the integration.
10. **No 0.17 adoption in core** (D33). Writing install records for 0.17
    installs is done by the separate migration tool (issue 023). The
    engine has no 0.17-specific code.
11. **Old code is deleted.** `gupkg.py`'s install and update coordinators,
    `updates.prepare_update` and its receipt-by-mtime logic,
    `origin.populate_*` and the `manager.py` executor are removed. Their
    behaviour is now covered by the engine.

## Acceptance criteria

- [ ] On `FakeSystem`, each of these operations has an end-to-end test of
      the resulting machine state:
  - install
  - install again (a repair; nothing is downloaded)
  - `check-updates`, which writes a version directory, downloads and
    populates its `App`, and leaves `current` and integration unchanged
  - a following `update`, which activates it without downloading
  - `check-updates --no-download`, then `update`, which populates and
    activates it
  - update via the registry
  - switch back
  - uninstall
  - uninstall with `--purge`
  - cleanup
- [ ] When a package version drops a `[[shortcut]]` and a `[[shim]]`,
      updating to it removes both from the system.
- [ ] Installing a version directory populated by the built-in download
      script, with no hooks, fetches only the URLs written in its
      `pkg.toml`. This is checked with the `FakeSystem` fetch log, which must
      contain no other URL.
- [ ] A version directory with a `[track]` table or a `track` hook is
      rejected before any work.
- [ ] **Who runs `check-updates` (D22, D31).**
  - Elevated, on a system package: the new version directory is written
    into the system root with a populated `App`, and the package stays on
    its old version.
  - Non-elevated, on a system package: it reports the new version and
    nothing under the system root changes, with or without
    `--no-download`.
  - Non-elevated, on a user package, with `--no-download`: only `pkg.toml`
    and the support files are written, and nothing is downloaded.
  - Elevated, on a user package: it refuses, says to use a normal shell,
    and nothing changes.
- [ ] `install` and `update` on a package with only `_check_updates` make no
      tracker request. The `FakeSystem` fetch log is empty.
- [ ] **A recoverable `App` is disposable** (design §4). For every fixture
      version that has a `download_url`, whatever its `shipped` value, the
      following leaves the package `installed` and healthy, with
      integration unchanged:
      1. delete the `App` of every version, including the current one;
      2. run `install --refresh` (or `gupkg check --fix`).

      The same holds for a version populated by a custom populate script
      while the script's inputs exist; with an input removed, repair fails
      and `cleanup` keeps that `App`. The check for bundled installs is an
      acceptance criterion of issue 033.
- [ ] **Custom populate scripts (D27).** A `.ps1`, a `.cmd` and an
      executable populate script each receive the context as JSON and
      populate `App`; a failing script leaves `current` and integration
      unchanged. A custom-populated install is not labelled unverified.
- [ ] **Distribution origin (D29, D40).** Installing a distribution folder
      with no registry fingerprint on disk labels it *origin not verified*;
      with a matching fingerprint, *verified against registry `<name>`*;
      with a mismatching one, the install is refused and nothing changes.
      After an install, changing a file in the version directory makes
      `gupkg check` report the mismatch. Copies of a marked distribution
      carry no Mark of the Web, and the receipt records the mark.
- [ ] **A shipped `App` is never lost.** For a version whose `[source]` has
      `[source] shipped = true`, explicit or by default when there is no
      `download_url`:
  - `cleanup` and plain `uninstall` keep its `App`;
  - `uninstall --purge` asks for an explicit confirmation (with `--no-input`
    it exits 5 and deletes nothing), which names the `App` as unrecoverable
    when there is no `download_url`;
  - `gupkg check` reports a modified `App` file, unless the package sets
    `record_manifest = false` or the global switch is off (D41).
  - With a `download_url` as well, the shipped `App` is still never dropped
    or replaced by `cleanup` or `install --refresh`. After the `App` is
    deleted by hand, repair recovers it from the `download_url`. Without a
    `download_url`, repair fails with `App is missing and no way to recover
    it.`
- [ ] When population fails after a download, `current` still points at the
      previous version, and the system state has not changed.
- [ ] `gupkg check` reports a deleted shim and a missing PATH entry, and
      `gupkg check --fix` restores both.
- [ ] A 3-package batch where package 2 fails still installs package 3, and
      both the summary and the log name the failure.
- [ ] Cancelling a 3-package batch while package 1 runs finishes package 1
      cleanly, leaves packages 2 and 3 unchanged, and reports them as
      skipped. A stop marker given to an elevated batch has the same effect.
- [ ] No operation runs `cleanup` unless asked: after `install`, `update`
      and `uninstall`, old version directories are still present.
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

## Notes for issues 029, 030, 031, 032 and 033 (from the implementation of 028)

- **What exists (029).** `gupkg.engine.lifecycle` has `check_updates`, `install`, `update` (with
  `download_only`), `switch`, `uninstall` (`purge`), `cleanup` (`keep`, `bundled`), `check` (`fix`) and `repair`,
  all over `run_batch(engine, requests)` and `plan(engine, requests)` (the `--plan` data). Build the
  `gupkg.engine.context.Engine` once per call: `system`, `env`, `load` (`gupkg.modes.settings_loader`),
  `confirm`, `yes`, `no_input`, `cancel` (use `stop_marker(path)` for the stop request), `log_path` (give it
  `lambda path: str(session.log.package_log(path))` so the receipt stores the log), `relaunched`, and `argv`,
  `gupkg_dir`, `gupkg_root` for the one elevation (without them the engine never relaunches). Results are
  `BatchResult` and `PackageResult` (`to_data()` is the `--json` shape; `exit_code`; `error` has `code`,
  `message`, `hint`, and for a checksum refusal `url` and `skippable`). `lifecycle.cleanup_root(engine, context,
  settings)` prunes a root's logs and old registry trees: call it once per root after a cleanup. Pass
  `[logs] keep_runs` and `keep_days` from the settings to `RunSession`. `gupkg logs <target>` lists
  `<package>\.gupkg\logs` (the receipt's `[run] log` names the last one). The CLI maps `--skip-checksum` to
  `Request.skip` through `parse_skip_flag`; `--in-place`, `--refresh`, `--purge`, `--keep`, `--bundled`,
  `--no-download`, `--fix` and `--mode` are `Request` fields; `gupkg shell` prints `ModeContext.path_line()`.
- **Scope item 11 ("Old code is deleted") is formally NOT met by 028, and 029 owns it.** The epic allows this
  ("Each issue is meant to be mergeable on its own behind the existing CLI until 029 switches the front end",
  issue 020): the 0.17 front end (`cli.py`, `commands.py`, `tui.py`, `manager_tui.py`, `manager.py`) still runs and
  still imports the old coordinators, so deleting them in 028 would break the shipped program. The new engine
  imports none of the modules to delete, except the registry transports named in the last item below. The
  complete deletion list, now also written into issue 029 (and issue 030 for the TUIs):
  - `gupkg.py` (`install_package`, `_run_update`, `update_package`), `updates.py` (`prepare_update`, its
    receipt-by-mtime logic), `origin.py` (`populate_*`), `components.py`, `downloads.py`, `github_releases.py`,
    `configuration.py`, `configfix.py`, `metadata.py`, `collection.py`, `distribution.py`, `layout.py`
    (`identity_from_path`, `installed_version` and what only the old front end uses), `pinning.py`, `outcome.py`
    (its renderers move to `report/`), `commands.py`, `cli.py`'s 0.17 command bodies, `bootstrap.py` and
    `hooklib.py` if nothing else imports them, `legacy_to_gupkg_toml.py` and `shortcuts_to_gupkg_toml.py`
    (they live on in `gupkg_migrate`);
  - `registry.py` (the 0.17 registry client) and the executor of `manager.py`;
  - for issue 030: `tui.py`, `manager_tui.py`, `tui_widgets.py`, `LineStream`;
  - before `registry.py` can go, move the transports `catalog/registry.py` imports from it
    (`archive_revision`, `archive_url`, `extract_archive`, `git_environment`, `git_remote_revision`,
    `is_git_source`, `require_trusted_cache`, `trusted_git`): they are the only thing that makes `registry.py`,
    `origin.py`, `updates.py`, `downloads.py`, `github_releases.py`, `configuration.py` and `metadata.py`
    reachable from the new code.
- **The fingerprint (031).** There is one implementation of the version-directory fingerprint,
  `gupkg.services.fingerprint` (issue 031, byte-exact contract in design section 17); the registry tool, the engine's
  install verification and `check` all call it. `gupkg.engine.fingerprint` keeps only the D41 `App` fingerprint
  (`app_files`, `aggregate_digest`, `diff_app`). An earlier version of this note said the registry tool must call
  `engine.fingerprint.version_fingerprint`; that function no longer exists.
- **Vendor installers are implemented by 028** (owner decision after the first review; design 8.4 and D39,
  issue 031's GIMP package depends on it): `gupkg.engine.installer`, the setting `hooks.allow_vendor_installers`,
  the label `runs vendor installer` in the plan (`populate.label`), the output and the receipt. Issue 029 shows the
  label in `list --details` from `INSTALLER_LABEL` (`gupkg.model.manifest`) and the manifest's `source.installer`.
  The rules and the risk are in design 13.4.
- **Locks are implemented by 028**: `gupkg.engine.locks`, `System.process_id` and `System.process_is_running`. The code
  `package-busy` (exit 3) is what a front end shows when another run holds the package.
- **Owners of what is not done.** The `[root_forwarder]` table, `requires_gupkg` and exit code 6, and the launchers belong
  to issue 033 (check-updates already carries a bundled gupkg and the launchers forward into a new version directory when
  the root has no forwarder). Issue 032 (hardening) owns: a memory cap and an operating-system-enforced "no network" for
  the regex worker (a job-object limit in `WindowsSystem`), and the residual race on the parent folders of a distribution
  copy in `distcopy` (it needs a `System` method that opens a path without following links and returns its final path).
- **Sandbox (029).** A program that the sandbox has no double for ends a package with the code
  `needs-real-run` (exit 3), never a crash; `gupkg pkg check --install` should print that and declare the
  doubles it needs.
- **Local fixes made on the way.** `render_pinned` now expands `${download_url}` (a tracker's
  `[source.checksum] sidecar = "${download_url}.sha256"` stayed a template); `defines_track` finds every way
  of binding `track` (loop, `with`, walrus, starred, match, `type`, `except as`, `global`) and ignores a bare
  annotation; a version-directory hook that exposes `track` once loaded is refused; `git` is resolved only
  from admin-only folders in an elevated run; `copy_distribution` is bounded (entries, bytes, depth, size of
  `pkg.toml`) and refuses a junction at `root\.gupkg\work`, `root\<name>` and `root\<name>\<version>`.
