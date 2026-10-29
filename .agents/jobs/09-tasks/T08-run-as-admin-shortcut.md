# T08: A "run as administrator" shortcut for the system manager

Issue 035: §4.4 ("To act in system mode"), D49.

## Goal

A `[[shortcut]]` can carry `run_as_admin = true`. gupkg's own package uses it to give the system scope a Start
Menu entry "gupkg manager (administrator)". Windows shows the UAC prompt; gupkg contains no elevation code.

## Steps

1. **Manifest** (`model/manifest.py`): add an optional boolean `run_as_admin` to `[[shortcut]]` (default `false`).
   Validate it as a boolean.
2. **`ShortcutSpec`** (`system/port.py`): add `run_as_admin: bool = False`.
3. **`system/windows.py`:** when writing a shortcut with `run_as_admin`, set the `SLDF_RUNAS_USER` flag (`0x2000`)
   through `IShellLinkDataList::SetFlags` on the same link object, before saving. Read the existing COM code first and
   follow its style. When reading a shortcut back, report the flag.
4. **`system/fake.py`:** store and return the flag.
5. **Effects** (`engine/effects.py`): pass the field through, include it in the recorded effect, and let drift checking
   compare it.
6. **gupkg's own package** (`src/pkg.toml`): add a second shortcut, for system installs only:
   ```toml
   [[shortcut]]
   name = "gupkg manager (administrator)"
   target = "%COMSPEC%"
   arguments = '/d /s /c ""$App\gupkg-manager.cmd" || pause"'
   description = "Manage the packages installed for all users"
   run_as_admin = true
   modes = ["system"]    # see step 7
   ```
7. **A shortcut that applies in one mode only** (owner's decision, 2026-10-28). Add an optional `modes` list to
   `[[shortcut]]`, the same key and form as the package's own `modes`. It defaults to the package's modes. A value that
   is not in the package's `modes` (or is `"portable"`) makes the definition invalid. In a run whose mode is not in the
   list, the shortcut is not created, and an existing one from an earlier version is removed like any dropped shortcut.

## New tests (each must fail on `main`, then pass)

- [ ] A manifest with `run_as_admin = "yes"` is invalid.
- [ ] A `[[shortcut]]` with `modes = ["system"]` in a package whose `modes = ["user"]` is invalid.
- [ ] Installing a package with `run_as_admin = true` on `FakeSystem` records a shortcut with the flag.
- [ ] gupkg's own `pkg.toml`, installed in system mode, creates both shortcuts. In user mode it creates only
      "gupkg manager".
- [ ] A `windows_integration` test writes a real `.lnk` with the flag into `tmp_path`, reads it back and checks the flag.

## Done when

- [ ] The tests above pass. The `windows_integration` test is run and passes on this machine; paste the output.
- [ ] Manual check `035-T08-1` in `tests/manual_smoke.md`: on a VM or test root, double-click the shortcut, see UAC, and
      see the manager title `system · <root>` (after T09).
- [ ] The full suite passes.
