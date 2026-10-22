# Issue 030: `gupkg-manager` — a separate manager program, and the UI redesign

Date: 2026-10-22
Priority: High (daily usability)
Epic: 020. Design: `_docs/rehaul_design.md` §15, §10.4, §13.2. Supersedes the remaining parts of issue 015.

## Problem

### The owner's requests

- "Manager mode should be there, but probably should be a completely
  separate program that knows how to call the CLI" (2026-10-22, decision D12).
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

1. **A separate program, `gupkg_manager/`**, shipped in the same bundle with
   its own `gupkg-manager.cmd` launcher and `gupkg-manager.exe` shim.
   - **It runs only on the bundled interpreter.** The launcher starts
     `python\python.exe -I -s -B` relative to itself and calls
     `enforce_bundled_runtime` on start, under the same rules as issue 018.
   - **It runs its sibling CLI.** It always calls the `gupkg.cmd` next to it
     in the same gupkg tree (`$App\gupkg.cmd` installed,
     `<version>\gupkg\gupkg.cmd` bundled), never a `gupkg` found on `PATH`. It
     refuses a CLI whose `"api"` version does not match.
   - **It is part of the bundle checks.** It is covered by `MANIFEST.sha256`
     (issue 016) and imported by the build probe (issue 019).
   - **It imports nothing from `gupkg`.** A test enforces this.
   - **It talks to gupkg only through the CLI machine interface** of issue
     029: `--json`, `--plan`, `--events-file` and `--no-input`.
   - **Its parts:**
     - `client.py` runs `gupkg` subprocesses and follows the events file;
     - `batch.py` does inventory → plan → confirm → grouped execution;
     - `policy.py` applies the checksum policy (issue 027);
     - `app/` is the Textual UI;
     - `cli.py` is the manager's own command line.
2. **Commands.**
   - `gupkg-manager` opens the browser.
   - `gupkg-manager PATH|NAME` opens the package view.
   - The non-interactive commands are `gupkg-manager check --all`,
     `gupkg-manager update --all [--mode M] [--yes]` and
     `gupkg-manager doctor`. They take the same `--json` option as the CLI, so
     schedules and CI can use them.
3. **Batches** (§13.2).
   1. Build the inventory from `gupkg list --json`.
   2. Plan with `gupkg update <targets> --plan --json`.
   3. Show the plan table and ask for one confirmation. Checksum prompts are
      asked per package.
   4. Run the user-mode targets in one CLI call, and the system-mode targets in
      **one** elevated CLI call (a single UAC prompt).
   5. Show the combined results.

   **Check and update stay separate operations** (D11).
   - The browser has separate *Check for updates* (`c`) and *Update* (`u`)
     actions.
   - After a check, the newly written versions are listed. They can be
     opened and reviewed before anything is installed.
4. **The UI**, one Textual `App` with three screens. It never exits to switch
   screens.
   - **Browser:** a `DataTable` of packages (name, version, mode, status,
     registry); `/` filters; actions are on keys.
   - **Package:** versions (local, registry, tracker), mode, link, data
     folder, last receipt, log.
   - **Run:** an append-only `RichLog` fed from the CLI's `--events-file`,
     with the same coloured phase headers as the console renderer (issue
     022) and an inline progress bar. The verdict and the summary are written
     **at the end of the log**.
5. **A fixed frame on every screen.**
   - The top line is a key-hint bar for the current screen.
   - The bottom line is a status footer: counts, registry revision and age,
     run state, and the log path.
6. **Prompts are modals.** These cover the checksum `ask`, elevation, and
   confirmation. The CLI is always called with `--no-input`; every decision
   is made in the manager before the call.
7. **A running operation is protected.** During a run, Esc asks "Cancel after
   the current package?". Cancelling stops scheduling further CLI calls. The
   CLI call in progress is allowed to finish.
8. **Launchers.** `gupkg-manager.cmd` (the package launcher in each version
   folder, delivered later by issue 033, which replaces `gupkg-tui.cmd`) finds
   a gupkg as in design §21.3, then runs `gupkg-manager "%~dp0."`. When the
   manager exits, the last run's summary and the log path are printed to the
   normal console (`[manager] keep_screen`).
9. **The style guide is rewritten.** `_docs/tui_style_guide.md` now requires
   the key-hint bar and the status footer, and keeps the list-first,
   frameless content area.
10. **Old code is deleted:** `tui.py`, `manager_tui.py`, `tui_widgets.py`,
    `manager.py`'s executor, and `LineStream`.

## Acceptance criteria

- [ ] `gupkg_manager` has no import of `gupkg` (enforced by a test). Every
      operation it performs appears as a `gupkg …` subprocess call in its
      run log under `<root>\.gupkg\logs\`.
- [ ] Every screen shows a key-hint line at the top and a status line at the
      bottom. This is checked with Textual `Pilot` at 80×24 and 200×50.
- [ ] In the run view, phase headers appear in order and in colour. The output
      follows the tail and can be scrolled back, and the verdict is the last
      line of the log.
- [ ] *Update all* with two user and two system targets, run from the system
      gupkg, shows one plan and one confirmation and makes exactly two CLI
      calls; elevation is requested once, for the system-target call. Run
      from a user-writable gupkg, it requests no elevation and reports the
      system targets as needing the system gupkg.
- [ ] *Check for updates* writes new version directories (proposals for system
      packages) and installs nothing. The browser then shows those packages as
      `outdated`.
- [ ] Opening a package from the browser, running an install and going back
      keeps the browser's filter and selection, without restarting the app.
- [ ] Fed a 2,000-line events file, the run view stays scrolled to the tail
      and keeps responding to key presses (Pilot).
- [ ] After `gupkg-manager` exits, the console still shows the summary and
      the log path.

## Tests

- **Manager logic:** tests against a fake `gupkg` executable that replays
  recorded `--json`, `--plan` and events files and records the argv it
  receives.
- **UI behaviour:** Pilot-driven tests that assert on visible text and on
  reachable actions, not on widget IDs. They replace the layout-string
  assertions in `tests/test_tui.py` and `tests/test_manager_tui.py`.
