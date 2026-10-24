# Issue 029: Unified command-line interface and machine interface

Date: 2026-10-22
Priority: Critical
Epic: 020. Design: `_docs/rehaul_design.md` §10, §11. Supersedes issue 013's grammar decisions.

> **Status (2026-10-24):** Implemented on `rehaul/issue029`. The 0.17 front end is deleted (item 12, including the moved registry
> transports, which now live in `gupkg.services.registrysource`); `pinning.py` is NOT on the deletion list: it is issue 027's
> module behind `gupkg pkg pin` (the list named it by mistake). Decisions taken inside the issue: the stop marker is a unique path
> announced in `run_start.stop_file` (design 10.4); `--plan` also exists for `link` and `self ...`; `link` is an engine operation;
> `requires_gupkg` and a newer `format` give exit code 6; `tui` and `manager` refuse an elevated shell (`migrate` does not) and no forwarder takes `--settings`/`--sandbox`;
> `allow_unc_file_urls` is not a setting (`file:` URLs naming a network host stay refused, design 13.4); the parent of an elevated
> relaunch waits for the child and returns its exit code (clamped to 0-6, else 4; the raw code is `child_raw_exit_code` in the
> document); the child is started without a shell and only after the files it loads first were checked admin-only. Not done: `pkg bundle` copies the verified bundle but writes no launchers (the template is
> issue 033's); Manual checks owed: `tests/manual_smoke.md`, "The unified command
> line and machine interface (issue 029)" items 1-13.

## Problem

The owner says the CLI and manager mode are "messed up in a way that is
difficult to reason about". Specifically:

- **Two grammars for the same thing.** Package commands take a path and
  `manager …` commands take a selector. They resolve scope differently and
  install wrappers in different places.
- **Root options only work before the command.** `--scope`, `--format`,
  `--pause` and `--allow-hook-dependency-install` are rejected after it, so
  `gupkg manager install vscode --scope user` is a parse error.
- **`--shim-linkage` defaults differently** in package and manager commands.
- **`--no-checksum` is uneven.** `manager install` lacks it, and
  `update --check-only` ignores it.
- **Dispatch order is ad hoc** (`cli._dispatch_manager`):
  - `init`, `repair-deps` and `update-deps` run before the configuration
    loads;
  - `install`, `registry`, `search` and `self` run before discovery;
  - `doctor` is "list with a flag".
- **Confirmation is written twice:** `input()` in `cli.py`, and again in the
  TUI.
- **Smaller problems:**
  - `self update` is really a repair;
  - `PackageRequest.command_line` re-encodes the grammar by hand;
  - `run_guarded` and `run_guarded_outcome` are near-duplicates;
  - `config-fix` falls back on matching error-message substrings
    (`commands.py:268`).

The owner's decisions (2026-10-22) that shape this issue:

- **D11:** checking and installing are separate operations.
- **D12:** the manager is a separate program that calls the CLI.
- **D21:** update discovery is `gupkg check-updates`; `gupkg check` is the
  health check and replaces `doctor`; `gupkg pkg check` stays the
  authoring validator.
- **D22, D23:** who runs `check-updates`, and what it writes.
- **D25:** the CLI asks before consequential actions.
- **D26:** `--events-file NAME` and the log locations.
- **D33, D34:** no 0.17 aliases in core; `gupkg tui` and `gupkg manager`
  are permanent forwarders.

## Scope

1. **The grammar.** Implement §10.2 exactly.
   - Targets are names or paths, in the selector grammar of §10.1.
   - Options go after the command.
   - `--mode` replaces `--scope`. Its default comes from `default_mode`,
     which is `auto` (§6.2).
   - Every mutating command takes `--yes`.
   - The CLI has **no `--all`, no interactive UI and no checksum prompts**.
     Several targets in one call run one after another.
2. **`gupkg check-updates <target>…`** (D21–D23) runs `_check_updates`
   trackers and writes pinned version directories. By default it also
   downloads and verifies each new version's payload (issue 027) and
   populates its `App`; the version is ready but not active. It never
   activates or integrates anything.
   - `--no-download` writes only the `pkg.toml`-and-support-files
     directory where the root is writable, and only reports otherwise.
   - **System packages:** an administrator runs it from an admin shell, and
     it writes straight into the system root. From a normal shell it
     implies `--no-download` for a system package and only reports. There
     are no proposals and no admission step.
   - **An elevated `check-updates` on a user package refuses** (D31).
   - **`update` and `install` never run a tracker.**
   - `update` activates and integrates the newest *known* version, and
     populates it first if it has no `App` yet.
3. **`gupkg check [<target>…] [--fix]`** (D21) is the health check of
   issue 028. It replaces `doctor`. `gupkg pkg check` stays the authoring
   validator.
4. **The machine interface** (§10.4) is a versioned, public contract
   (`"api": 1`):
   - **`--json`:** one result document on stdout, with structured errors;
     human output goes to stderr.
   - **`--plan`:** resolves and plans without changing anything. Per target it
     reports version, mode, whether elevation is needed, checksum present or
     missing, the URL and the effects.
   - **`--events-file NAME`** (D26): the event stream as JSON lines,
     flushed per event, ending with the result. `NAME` is a plain file
     name, not a path. The CLI writes the stream into the log directories
     (issue 022): per-package information under
     `<root>\<name>\.gupkg\logs\`, global run information under
     `<root>\.gupkg\logs\`, and a bundled gupkg's runs that involve no
     package under its own package's `.gupkg\logs\`. The caller reads it
     there. This works across an elevation boundary, because the elevated
     child chooses the directory itself.
   - **`--no-input`:** never prompts. A needed answer becomes a structured
     error, with exit code 5.
5. **`cli.py` stays thin.**
   - It parses the arguments, loads the settings, builds the reporter
     (issue 022), calls `engine.lifecycle` or `catalog`, renders the
     `Result`, and returns the exit code.
   - Exit codes: 0, 2, 3, 4, 5 for declined or input needed, and 6 when
     the package needs a newer gupkg (issue 033).
   - **Confirmations** (D25). The CLI asks before consequential actions:
     `uninstall --purge`, a plan with several targets, and anything
     destructive. They go through the console renderer. `--yes` answers
     them, and under `--no-input` a question becomes exit code 5. The CLI
     never asks about checksums.
6. **`gupkg settings show|path|init|set`.** Converting the 0.17 manager
   config is the separate migration tool's job (issue 023, D33).
7. **`gupkg self status|repair|check-updates|update`** (design §20, D14).
   - `self check-updates` and `self update` are aliases for `gupkg
     check-updates gupkg` and `gupkg update gupkg`. A self-update is
     therefore checked first, then installed, like any package (D11).
   - `self status` reports the bundle's `MANIFEST.sha256` check (issue 016)
     and the interpreter in use (issue 018).
   - `self repair` re-applies gupkg's own integration.
   - The `repair-deps` and `update-deps` commands are gone, per issue 016.
8. **No 0.17 aliases** (D33). Core has no deprecated spellings and no
   format 1 hints. `config-check`, `config-fix`, `doctor`, `repair-deps`
   and `update-deps` are unknown commands, and `update --check-only` and
   `--scope` are usage errors.
9. **Permanent forwarders to the separate tools** (D34, D45). They are
   part of the 1.0 grammar, not aliases:
   - `gupkg tui PATH` runs `gupkg-tui PATH`;
   - `gupkg manager …`, including `gupkg manager tui`, runs
     `gupkg-manager …` with the same arguments;
   - `gupkg migrate …` runs `gupkg-migrate …`, the migration tool
     (issue 023), with the same arguments.

   A forwarder passes the exit code through.
10. **`commands.py` is deleted.** Its remaining responsibilities move to
    `engine`, `catalog` and `selfmgmt`. `outcome.py` becomes the `Result`
    renderers in `report/`.
11. **Help text.** `gupkg --help` lists the commands in task order:
    check-updates/install/update/list/info first, then maintenance
    (including `check`), then authoring, then self. Every subcommand has an
    example.
12. **Delete the 0.17 coordinators (handed over by issue 028, whose scope item 11 stays open until this lands).**
    Issue 028 built the engine beside the 0.17 front end, as the epic allows ("mergeable on its own behind the existing CLI
    until 029 switches the front end"), so the old code is still imported by the front end that this issue replaces. When
    the front end moves to `gupkg.engine.lifecycle`, delete, in one commit, with their tests:
    - `gupkg.py` (`install_package`, `_run_update`, `update_package`), `updates.py` (`prepare_update` and the
      receipt-by-mtime logic), `origin.py` (`populate_*`), `components.py`, `downloads.py`, `github_releases.py`,
      `configuration.py`, `configfix.py`, `metadata.py`, `collection.py`, `distribution.py`, `layout.py` (what only the old
      front end uses, including `identity_from_path` and `installed_version`), `outcome.py`, `commands.py`,
      the 0.17 command bodies of `cli.py`, `registry.py` (the 0.17 registry client), the executor of `manager.py`, and
      `legacy_to_gupkg_toml.py` and `shortcuts_to_gupkg_toml.py` (the migration tool `gupkg_migrate` has its own copies);
      check `bootstrap.py` and `hooklib.py` for remaining importers first;
    - **first** move the transports that `catalog/registry.py` imports from `registry.py` (`archive_revision`,
      `archive_url`, `extract_archive`, `git_environment`, `git_remote_revision`, `is_git_source`,
      `require_trusted_cache`, `trusted_git`): they are the only reason `registry.py`, `origin.py`, `updates.py`,
      `downloads.py`, `github_releases.py`, `configuration.py` and `metadata.py` are still reachable from the engine;
    - `tui.py`, `manager_tui.py`, `tui_widgets.py` and `LineStream` go with issue 030.
    A test that imports every remaining module under the bundled interpreter proves nothing still needs them.

## Acceptance criteria

- [x] `gupkg tui PATH` runs `gupkg-tui PATH`, and `gupkg manager <args>`
      (including `gupkg manager tui`) runs `gupkg-manager <args>`, and each
      returns the program's exit code. This is checked with fake
      `gupkg-tui` and `gupkg-manager` executables that record their argv,
      so it does not wait for issue 030. `config-check`, `config-fix`,
      `doctor`, `repair-deps` and `update-deps` are unknown commands, and
      `update --check-only` and `--scope` are usage errors (exit 2).
- [x] `gupkg install vscode --mode user` and `gupkg install
      %USERPROFILE%\opt\vscode` give identical integration destinations,
      because both use the `ModeContext` from the settings.
- [x] With the system gupkg in an elevated process, `gupkg install X` with
      no `--mode` installs in
      system mode and prints `system (auto: elevated)`. From a normal shell it
      installs in user mode.
- [x] `gupkg check-updates X` on a tracked user package writes
      `X\<new version>\` with a populated `App`, and leaves `current`, the
      install record and the `FakeSystem` integration state unchanged. With
      `--no-download` it writes the directory with no `App` and makes no
      request except the tracker's.
- [x] `gupkg update X` after `check-updates X --no-download` installs the
      new version without contacting any URL except those written in the
      new version's `pkg.toml`. This is verified with the `FakeSystem`
      fetch log.
- [x] `gupkg self check-updates` and `gupkg check-updates gupkg` give the
      same result document.
- [x] `gupkg check X` reports a deleted shim of an installed package as
      drift and changes nothing; `gupkg check X --fix` restores it.
- [x] `gupkg update X`, when no newer version is known, reports "up to date"
      and makes no network request.
- [x] `--plan --json` changes nothing on disk except the run's log files, and
      the document names every target's checksum status and elevation need.
- [x] A run with `--events-file NAME` from an elevated child produces a
      JSON-lines file named `NAME` in the system root's log directories,
      which a non-elevated reader can follow to the final result line.
      `--events-file` with a value that contains a path separator or `..`
      is a usage error (exit 2) and writes nothing.
- [x] `gupkg uninstall X --purge --no-input` exits 5 with a structured error
      and changes nothing; with `--yes` it proceeds without reading stdin.
      The same holds for `gupkg install X Y` (a plan with several targets).
- [x] `gupkg update --all` and `gupkg update --check` are usage errors (exit
      2); the first names `gupkg-manager update --all` (D11, D12).

## Tests

- CLI behaviour tests run through `main(argv)` against `FakeSystem`. They
  check exit codes, the visible stderr summary and the stdout JSON.
- Contract tests pin the `--json`, `--plan` and `--events-file` document
  shapes. The manager (issue 030) relies on them.
