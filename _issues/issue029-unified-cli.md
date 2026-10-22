# Issue 029: Unified command-line interface and machine interface

Date: 2026-10-22
Priority: Critical
Epic: 020. Design: `_docs/rehaul_design.md` §10, §11. Supersedes issue 013's grammar decisions.

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

## Scope

1. **The grammar.** Implement §10.2 exactly.
   - Targets are names or paths, in the selector grammar of §10.1.
   - Options go after the command.
   - `--mode` replaces `--scope`. Its default comes from `default_mode`,
     which is `auto` (§6.2).
   - Every mutating command takes `--yes`.
   - The CLI has **no `--all`, no interactive UI and no checksum prompts**.
     Several targets in one call run one after another.
2. **`gupkg check <target>…`** runs `_check_updates` trackers and writes
   pinned version directories, or system-root proposals (§9.3). It never
   installs anything.
   - **`update` and `install` never run a tracker.**
   - `update` installs the newest *known* version.
   - An elevated `update` admits pending system-root proposals after
     confirmation.
3. **The machine interface** (§10.4) is a versioned, public contract
   (`"api": 1`):
   - **`--json`:** one result document on stdout, with structured errors;
     human output goes to stderr.
   - **`--plan`:** resolves and plans without changing anything. Per target it
     reports version, mode, whether elevation is needed, checksum present or
     missing, the URL and the effects.
   - **`--events-file PATH`:** the event stream as JSON lines, flushed per
     event, ending with the result. This works across an elevation boundary.
   - **`--no-input`:** never prompts. A needed answer becomes a structured
     error, with exit code 5.
4. **`cli.py` stays thin.**
   - It parses the arguments, loads the settings, builds the reporter
     (issue 022), calls `engine.lifecycle` or `catalog`, renders the
     `Result`, and returns the exit code.
   - Exit codes: 0, 2, 3, 4, and 5 for declined or input needed.
   - The only prompts left are confirmations, such as `uninstall --purge`
     and admitting a proposal. They go through the console renderer and are
     answered by `--yes` or refused by `--no-input`.
5. **`gupkg settings show|path|init|set`.** `init --from
   <gupkg-config.toml>` converts manager schema 2 (issue 032).
6. **`gupkg self status|repair|check|update`** (design §20, D14).
   - `self check` and `self update` are aliases for `gupkg check gupkg` and
     `gupkg update gupkg`. A self-update is therefore checked first, then
     installed, like any package (D11).
   - `self status` reports the bundle's `MANIFEST.sha256` check (issue 016)
     and the interpreter in use (issue 018).
   - `self repair` re-applies gupkg's own integration.
   - The `repair-deps` and `update-deps` commands are gone, per issue 016.
7. **The 0.17 spellings** are deprecated aliases during 1.0.x. They print the
   new spelling and run it, and are removed in 1.1:

   | 0.17 spelling | 1.0 behaviour |
   | --- | --- |
   | `gupkg manager <sub>` | the CLI equivalent, or a hint pointing to `gupkg-manager` for `tui` and `update` (all) |
   | `gupkg tui PATH` | `gupkg-manager PATH` |
   | `config-check` / `config-fix` | `gupkg pkg check` / `gupkg pkg migrate` |
   | `update --check-only` | `check` |
   | `--scope` | `--mode` |

8. **`commands.py` is deleted.** Its remaining responsibilities move to
   `engine`, `catalog`, `selfmgmt` and `migrate`. `outcome.py` becomes the
   `Result` renderers in `report/`.
9. **Help text.** `gupkg --help` lists the commands in task order:
   check/install/update/list/info first, then maintenance, then authoring,
   then self. Every subcommand has an example.

## Acceptance criteria

- [ ] Every row of the §10.3 table works, and each old spelling prints the new
      one during 1.0.x.
- [ ] `gupkg install vscode --mode user` and `gupkg install C:\opt\vscode`
      give identical integration destinations, because both use the
      `ModeContext` from the settings.
- [ ] From an elevated shell, `gupkg install X` with no `--mode` installs in
      system mode and prints `system (auto: elevated)`. From a normal shell it
      installs in user mode.
- [ ] `gupkg check X` on a tracked package writes `X\<new version>\` with no
      `App`, and changes nothing else.
- [ ] `gupkg update X` after that installs the new version without contacting
      any URL except the new version's `[source]`. This is verified with the
      `FakeSystem` fetch log.
- [ ] `gupkg update X`, when no newer version is known, reports "up to date"
      and makes no network request.
- [ ] `--plan --json` changes nothing on disk, and the document names every
      target's checksum status and elevation need.
- [ ] A run with `--events-file` from an elevated child produces a JSON-lines
      file that a non-elevated reader can follow to the final result line.
- [ ] `rg "input\(|isatty" src/gupkg/cli.py` finds nothing: confirmations
      live in the renderer.

## Tests

- CLI behaviour tests run through `main(argv)` against `FakeSystem`. They
  check exit codes, the visible stderr summary and the stdout JSON.
- Contract tests pin the `--json`, `--plan` and `--events-file` document
  shapes. The manager (issue 030) relies on them.
