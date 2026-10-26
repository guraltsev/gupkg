# T12: Documentation and consistency sweep

Issue 035: §8 step 8, §11. AGENTS rule 13.

## Goal

Every document and every help text describes the code as it is after T01–T11. Every mismatch you find is listed.

## Steps

1. **User docs.** Update for the new model: one installation per run, `--root`, no `--mode`, portable packages, the
   elevated manager, the launcher order, settings schema 4. The files:
   - `README.md` and `src/gupkg/README.md`;
   - `_docs/getting_started.md`, `_docs/cookbook.md`, `_docs/operations.md`, `_docs/troubleshooting.md`;
   - `_docs/README.md`, `_docs/development_guide.md`, `_docs/tui_style_guide.md` (the title shows side and root).
2. **Design.** Compare `_docs/rehaul_design.md` (T00's version) with the code. Fix every place where the merged code
   chose differently, and name each one in your log (for example the shortcut key name chosen in T08, or the effect
   kind chosen in T11).
3. **Help text.** Run `gupkg --help` and the `--help` of every command from the source tree, plus
   `gupkg-manager --help` and `gupkg-tui --help`. Compare each with the docs. Fix wrong help strings in the code.
4. **TUI key hints.** Render the manager and the package view (Textual pilot). Compare the hint line with the real key
   bindings and with the `?` help screen. Paste the screens into your log.
5. **Issue files:**
   - add to the top of `_issues/issue035-one-installation-per-run.md`:
     `> **Status (<date>):** Implemented by job 09, tasks T00–T12. Acceptance: T13.`;
   - add a status line to issue 034 saying it is superseded by issue 035, and mark which of its items were done where.
6. **Final greps.** Each must print nothing outside `_docs/rehaul_design.md`'s superseded decision rows and the closed
   issues:
   - `git grep -n -i "default_mode\|--mode\|--in-place\|ad-hoc\|adhoc\|forwarder\|--no-system-gupkg\|relaunch\|gupkg migrate\|opt-portable\|\[roots\]\.portable"`
   - `git grep -n "mode:name\|system:\w\|user:\w" -- _docs README.md`

## Done when

- [ ] Both greps are clean (paste the output).
- [ ] Your log lists every inconsistency found, and for each one: fixed here, or left (and why).
- [ ] The rendered screens are in your log.
- [ ] The full suite passes.
