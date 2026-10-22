# Issue 030: `gupkg-tui` and `gupkg-manager` — separate UI programs, and the UI redesign

Date: 2026-10-22
Priority: High (daily usability)
Epic: 020. Design: `_docs/rehaul_design.md` §15, §10.4, §13.2. Supersedes the remaining parts of issue 015.

## Problem

### The owner's requests

- "Manager mode should be there, but probably should be a completely
  separate program that knows how to call the CLI" (2026-10-22, decision D12).
- There are two UI programs, both specified here (D34): `gupkg-tui` for one
  package, and `gupkg-manager` for everything else.
- "Textual feels clunky." Printing success at the top of the output is
  "ridiculous". The owner wants:
  - a short key guide at the top;
  - output below it that scrolls naturally;
  - install phases clearly marked with colour and emphasis.

### What the 0.17 manager does wrong

#### Structure

- **The manager is a mode inside the CLI process.** It imports the
  workflows directly and redirects the process-global stdout.
- **Bulk updates throw per-package output away** (`manager._quiet`).
- **Opening a package restarts the application.** It exits the manager app
  and runs a new package app in a `while True` loop
  (`manager_tui.py:810-818`), so the screen flickers and state is lost.

#### Layout

- **The verdict and the result are split.** The verdict is in a `#status`
  line at the top, and the outcome is at the bottom, in CLI jargon
  (`tui.py:342-389`).
- **There are no key hints.** The style guide forbids headers, footers and
  persistent help (`_docs/tui_style_guide.md:58,134,210,292`).
- **The "MANAGER MODE" label is invisible**, because pushed screens cover it.
- **An empty "--- Navigation ---" section** appears in forced-scope mode.

#### Rendering and threads

- **The whole output is re-rendered for every line** (`OutputView.set_lines`).
- **Widgets are touched from worker threads** (`tui.py:371`,
  `manager_tui.py:515,526,585`).

#### Running operations

- **Leaving a running operation is unprotected.** Enter or Esc pops the
  screen while the worker keeps running (`tui.py:330`).
- **Escape never leaves `ExecutionScreen`** (`manager_tui.py:723`).

## Scope

1. **Two separate programs** (D34), shipped in the same bundle:
   - **`gupkg_tui/`**, the single-package program `gupkg-tui`;
   - **`gupkg_manager/`**, the general program `gupkg-manager`.

   Each has its own launcher next to `gupkg.cmd` in the gupkg tree
   (`gupkg-tui.cmd`, `gupkg-manager.cmd`) and its own `.exe` shim. Both
   follow the same rules:
   - **They run only on the bundled interpreter.** The launcher starts
     `python\python.exe -I -s -B` relative to itself and calls
     `enforce_bundled_runtime` on start, under the same rules as issue 018.
   - **They run their sibling CLI.** They always call the `gupkg.cmd` next
     to them in the same gupkg tree (`$App\gupkg.cmd` installed,
     `<version>\gupkg\gupkg.cmd` bundled), never a `gupkg` found on `PATH`.
     They refuse a CLI whose `"api"` version does not match.
   - **They are part of the bundle checks.** They are covered by
     `MANIFEST.sha256` (issue 016) and imported by the build probe (issue
     019).
   - **They import nothing from gupkg core** (D12, D34), and **talk to
     gupkg only through the CLI machine interface** of issue 029: `--json`,
     `--plan`, `--events-file` and `--no-input`. Both rules are guarded by
     behaviour tests (D38), not by module-boundary tests.
   - **`gupkg_manager`'s parts:**
     - `client.py` runs `gupkg` subprocesses and follows the events file;
     - `batch.py` does inventory → plan → confirm → grouped execution;
     - `policy.py` applies the checksum policy (issue 027);
     - `app/` is the Textual UI;
     - `cli.py` is the manager's own command line.

     `gupkg_tui` has the same client, policy and UI parts for one package,
     and no batch.
2. **Commands.**
   - `gupkg-tui PATH|NAME` opens the package view of one package.
     `gupkg tui PATH` forwards to it (issue 029).
   - `gupkg-manager` and `gupkg-manager tui` open the browser.
     `gupkg manager …`, including `gupkg manager tui`, forwards to
     `gupkg-manager …` (issue 029). The forwarders are permanent.
   - The manager's non-interactive commands are `gupkg-manager
     check-updates --all`, `gupkg-manager update --all [--mode M] [--yes]`
     and `gupkg-manager check` (the health check, D21). They take the same
     `--json` option as the CLI, so schedules and CI can use them.
3. **Batches** (§13.2).
   1. Build the inventory from `gupkg list --json`.
   2. Plan with `gupkg update <targets> --plan --json` (or `check-updates`).
   3. Show the plan table and ask for one confirmation. Checksum prompts are
      asked per package.
   4. Run the user-mode targets in one CLI call, and the system-mode targets in
      **one** elevated CLI call (a single UAC prompt). The manager may relay
      `--skip-checksum` to the elevated call (D24, `https://` URLs only).
   5. Show the combined results.
   6. With `[cleanup] auto = true`, run `gupkg cleanup` after `update --all`.
      `cleanup` never runs otherwise (D43).

   **Check and update stay separate operations** (D11, D23).
   - The browser and the package view have separate *Check for updates*
     (`c`) and *Update* (`u`) actions.
   - *Check for updates* runs `gupkg check-updates`, which writes and
     populates the new versions but activates nothing. System packages are
     checked in the elevated call, which writes into the system root (D22).
     Run from a user-writable gupkg, the manager runs no elevated call, and
     the system packages are only reported.
   - After a check, the newly written versions are listed. They can be
     opened and reviewed before anything is installed.
4. **The UI.** Each program is one Textual `App` that never exits to switch
   screens.
   - **Browser** (`gupkg-manager` only): a `DataTable` of packages (name,
     version, mode, status, registry); `/` filters; actions are on keys.
   - **Package** (both programs): versions (local, registry, tracker), mode,
     link, data folder, last receipt, log.
   - **Run** (both programs): an append-only `RichLog` fed from the CLI's
     `--events-file`, with the same coloured phase headers as the console
     renderer (issue 022) and an inline progress bar. The verdict and the
     summary are written **at the end of the log**.
5. **A fixed frame on every screen.**
   - The top line is a key-hint bar for the current screen.
   - The bottom line is a status footer: counts, registry revision and age,
     run state, and the log path.
6. **Prompts are modals.** These cover the checksum `ask`, elevation, and
   confirmation. The CLI is always called with `--no-input`; every decision
   is made in the UI before the call, and the call carries `--yes` for the
   confirmations already given (D25).
7. **A running operation is protected** (D37). During a run, Esc asks
   "Cancel after the current package?". Cancelling asks the running CLI to
   stop after its current package: the CLI finishes that package cleanly
   and skips the rest. An elevated call is asked through a stop marker,
   which can only make it stop. No further CLI calls are started.
8. **Launchers.** `gupkg-tui.cmd` (the package launcher in each version
   folder, delivered later by issue 033) finds a gupkg as in design §21.3,
   then runs `gupkg tui "%~dp0."` through it, which forwards to `gupkg-tui`
   in that gupkg's tree. When the program exits, the last run's
   summary and the log path are printed to the normal console
   (`[manager] keep_screen`).
9. **The style guide is rewritten.** `_docs/tui_style_guide.md` now requires
   the key-hint bar and the status footer, and keeps the list-first,
   frameless content area.
10. **Old code is deleted:** `tui.py`, `manager_tui.py`, `tui_widgets.py`,
    `manager.py`'s executor, and `LineStream`.

## Acceptance criteria

- [ ] **Only the CLI (D38).** With gupkg core not importable and only the
      `gupkg` CLI available, `gupkg-tui` and `gupkg-manager` open, plan,
      install and check as usual. Every operation they perform appears as a
      `gupkg …` subprocess call in their run log under
      `<root>\.gupkg\logs\`.
- [ ] `gupkg-tui PATH` opens the package view of that package, and
      `gupkg-manager tui` opens the browser.
- [ ] Every screen of both programs shows a key-hint line at the top and a
      status line at the bottom. This is checked with Textual `Pilot` at
      80×24 and 200×50.
- [ ] In the run view, phase headers appear in order and in colour. The output
      follows the tail and can be scrolled back, and the verdict is the last
      line of the log.
- [ ] *Update all* with two user and two system targets, run from the system
      gupkg, shows one plan and one confirmation and makes exactly two CLI
      calls; elevation is requested once, for the system-target call. Run
      from a user-writable gupkg, it requests no elevation and reports the
      system targets as needing the system gupkg.
- [ ] *Check for updates* with one user and one system package, run from
      the system gupkg, writes and populates both new version directories,
      the system one through the single elevated call, and activates
      nothing. The browser then shows both packages as `outdated`.
- [ ] Cancelling a 3-package run while package 1 runs lets package 1
      finish, leaves packages 2 and 3 unchanged, and reports them as
      skipped.
- [ ] With `[cleanup] auto = true`, `gupkg-manager update --all` ends with a
      cleanup; without it, old version directories are kept.
- [ ] Opening a package from the browser, running an install and going back
      keeps the browser's filter and selection, without restarting the app.
- [ ] Fed a 2,000-line events file, the run view stays scrolled to the tail
      and keeps responding to key presses (Pilot).
- [ ] After `gupkg-tui` or `gupkg-manager` exits, the console still shows the
      summary and the log path.

## Tests

- **Program logic:** tests against a fake `gupkg` executable that replays
  recorded `--json`, `--plan` and events files and records the argv it
  receives.
- **UI behaviour:** Pilot-driven tests that assert on visible text and on
  reachable actions, not on widget IDs. They replace the layout-string
  assertions in `tests/test_tui.py` and `tests/test_manager_tui.py`.
