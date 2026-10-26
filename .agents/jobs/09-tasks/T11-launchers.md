# T11: Launchers: bundled, then `gupkg-location`, then `PATH`

Issue 035: §4.9, D52. Owner answers Q4 and Q7 (b). These are `.cmd` files: test them with real `cmd.exe` in `tmp_path`,
the way `tests/test_package_launcher.py` already does.

## Goal

A package launcher (`install.cmd`, `gupkg-tui.cmd`, `check-updates.cmd`, all generated from
`src/gupkg/launchers/package-launcher.cmd`) chooses its gupkg in this order:

1. **Bundled:** `%~dp0gupkg\gupkg.cmd`.
2. **The root's setting:** the file `<root>\.gupkg\gupkg-location` (`<root>` is `%~dp0..\..`).
   - Its first line is a folder that contains `gupkg.cmd`. A relative path is relative to `<root>`.
   - Read it with `set /p`, and never execute it.
   - A missing file, an empty file, or a folder without `gupkg.cmd` means "skip". Print one line saying which file was
     skipped and why.
3. **`PATH`:** the first `gupkg.cmd` or `gupkg.exe` found by `where`.
   - **When elevated**, take the `PATH` entries in order and keep an entry only if its folder passes the existing
     admin-only check. Print each skipped entry. Never read the Windows registry.
   - For a `gupkg.exe` shim, the check covers the shim, its `<name>.config.toml` and the target the config names, plus
     that target's gupkg tree, recursively.
4. **Else** exit 9009 with the bootstrap hint (unchanged text).

When elevated, the existing ACL check also covers `<root>\.gupkg\` and the `gupkg-location` file itself.

**Exit code 6** (this gupkg is too old) moves on to the next step in this order. Today it falls back to the bundled copy;
that reverses.

## Remove

- **The root forwarder:**
  - the `[root_forwarder]` table in `model/manifest.py`;
  - its effect in `engine/effects.py` (write and remove);
  - its use in `src/pkg.toml`;
  - every reference to `.gupkg\gupkg.cmd` / `.gupkg\gupkg-manager.cmd` in the launcher template and in `gupkg_ui`.
- **The option `--no-system-gupkg`** in the template and in its docs/help.

## Add

1. **Installing the `gupkg` package writes `<root>\.gupkg\gupkg-location`,** containing `gupkg\current\App` (relative
   to the root). It is recorded in the install record as an effect, so uninstall removes it, but only if the file still
   holds that value. Implement it as a new effect kind `location_file`, or reuse `file` if that fits. Explain your
   choice in the log.
2. **`cleanup --bundled`** removes bundled copies only where step 2 or 3 would find a gupkg. Today the condition is
   "where a forwarder exists".
3. **Regenerate the launchers** written by `gupkg pkg bundle`, `pkg export`, `check-updates` and `pkg new`. They all use
   the one template.

## New tests (each must fail on `main`, then pass)

These use real `cmd.exe` and fake gupkg trees in `tmp_path` whose `gupkg.cmd` echoes its own path:

- [ ] With a bundled copy and a `gupkg-location`: the bundled one runs.
- [ ] Without a bundled copy: the one named in `gupkg-location` runs.
- [ ] With a relative `gupkg-location`: it is resolved against the root.
- [ ] With an empty `gupkg-location`, and with one naming a folder without `gupkg.cmd`: the launcher prints the skip line
      and uses `PATH`.
- [ ] With nothing at all: exit 9009 and the hint.
- [ ] The bundled copy exits 6: the next step runs.
- [ ] Elevated (mark `needs_admin`, like the existing elevated launcher tests): a user-writable folder on `PATH` is
      skipped and named. A shim whose config file is user-writable is refused.
- [ ] Installing the gupkg package on `FakeSystem` writes `gupkg-location`. Uninstall removes it. Uninstall leaves it
      alone if it was edited.

## Done when

- [ ] `git grep -n "root_forwarder\|no-system-gupkg\|\.gupkg\\\\gupkg.cmd\|\.gupkg\\\\gupkg-manager"` prints nothing.
- [ ] The `needs_admin` tests have been run from an elevated shell, **or** they are listed as manual check `035-T11-1`
      in `tests/manual_smoke.md`.
- [ ] The full suite passes.
