# T09: The manager has one scope per run

Issue 035: §3.1 item 8, §4.5, D53. Read `_docs/tui_style_guide.md` first. AGENTS rule 8 applies: show every screen
change.

## Goal

An elevated manager manages the system installation, and a normal one the user's. The UI shows which. Nothing in
`gupkg_ui`, `gupkg_tui` or `gupkg_manager` reasons about two sides.

## Remove

- **`gupkg_ui/startup.py`:** the elevated-shell refusal (~117–119).
- **`gupkg/cli.py`:** the elevated-shell refusal of the `tui`/`manager` forwarders (`_forward`, ~1186–1192).
- **`gupkg_ui/steps.py`:** `ELEVATING`, `NEEDS_SYSTEM_GUPKG`, `can_elevate`, the two-group logic of `make_steps` and the
  `labels` parameter. `make_steps` returns **one** step per distinct `--skip-checksum` argument. Usually that is one step.
  If the file shrinks to almost nothing, fold it into `flows.py`.
- **`gupkg_ui/settings.py`:**
  - `checksum_policy(mode)` becomes a property `checksum_policy`;
  - remove `root_of(mode)` and `default_mode`;
  - `load_ui_settings` uses T05's `build_installation`.
- **`UiEnvironment.can_elevate`** and every use of it.
- **`Subject.mode`, `PackageRow.mode`,** and the Mode column of the browser (`gupkg_manager/app/browser.py`).
- **`gupkg_manager/cli.py`:** `--mode` of `update --all`, if still present. Also `batch.py`'s `pick(..., mode=)`.
- **`gupkg_ui/session.py`:** any `elevated` step handling.

## Add

1. **`UiEnvironment.installation`:** the run's `Installation`.
2. **The title bar** of `gupkg-manager` and `gupkg-tui` shows `system · <root>` or `user · <root>`.
3. **The status line** counts only this installation's packages and shows this installation's registry state.

## New tests (each must fail on `main`, then pass)

- [ ] `build_environment` with an elevated `FakeSystem` and an admin-only gupkg location succeeds, and its installation
      side is `system`.
- [ ] The rendered browser (Textual pilot, `export_text`) shows the title with side and root, has no Mode column, and
      its status line matches the fake CLI's `list --json`.
- [ ] An update of three packages makes **one** CLI call, with all three targets (use the recording test double in
      `tests/ui_support.py`).
- [ ] `gupkg manager` from an elevated `FakeSystem` starts, and does not refuse.

## Done when

- [ ] `git grep -n -i "can_elevate\|NEEDS_SYSTEM_GUPKG\|ELEVATING\|elevated shell\|\.mode\b" -- src/gupkg_ui src/gupkg_manager src/gupkg_tui`
      prints nothing. Check every remaining `.mode` by hand and list it.
- [ ] Your log contains the rendered screen of the browser, before and after: full text, including the hint line and
      the status line.
- [ ] The full suite passes.
