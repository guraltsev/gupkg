# T04: Settings schema 4 and the root fallback chain

Issue 035: §4.1 (the fallback table), §4.2, D48, D50. Owner answer Q1.

## Goal

Settings are split by mode, and the root of each mode is found by a fixed chain of fallbacks. In this task you build
and test the pieces as functions. **T05 wires them into the CLI.** After this task the old code paths may still exist
beside the new functions, but they must not use the new schema incorrectly.

## The rules to implement

**Root of the system mode**: the first that applies:
1. `--root DIR` (it must be admin-only, else `SettingsError` with the reason);
2. the root of the installed system gupkg that runs (today's `installed_system_root`; see `settings_loader` callers in
   `cli.py` and `gupkg_ui/settings.py`);
3. `[roots].system` in `$App\settings.toml`;
4. built-in `C:\opt`.

**Root of the user mode**: the first that applies:
1. `--root DIR`;
2. `[roots].user` in the `--settings` file;
3. the root the running gupkg is installed in, when that root is **not** admin-only (issue 035 §4.1, Q11). A gupkg
   started from the source tree or a bundled copy has no such root, and the row does not apply;
4. `[roots].user` in `$App\settings.toml`;
5. built-in `%USERPROFILE%\opt`.

**Settings layers of a run**, lowest priority first:
1. built-in defaults;
2. `$App\settings.toml`;
3. `<root>\.gupkg\settings.toml`;
4. `--settings PATH`, in user mode only. The system mode raises `SettingsError` when it is given.

In system mode, every file layer must be in an admin-only folder. Keep the existing check.

**`[roots]` may appear only in layers 2 and 4.** In a root's own `settings.toml` it is a `SettingsError`:
`[roots] cannot be set in a root's own settings.toml (<path>); set it in $App\settings.toml or with --root`.

**Schema 4** (`model/settings.py`):

```toml
schema = 4
[roots]                 # only in $App\settings.toml or --settings
system = 'C:\opt'
user = '%USERPROFILE%\opt'
[integration]           # one table; its defaults depend on the mode
bin = '...'             # default: system C:\bin, user %USERPROFILE%\bin (or the machine's own default folders)
start_menu = '...'
integrate = "full"      # full | none
```

Remove `default_mode`, `[integration.system]` and `[integration.user]`. A file with `schema = 3` is a `SettingsError`
that says: `settings schema 3 is no longer read; change it to schema = 4 (see _docs/rehaul_design.md §11)`. There is no
code that converts it.

## Steps

1. In `model/settings.py`, add the schema 4 table and defaults. A function `default_layer(mode)` returns the mode's
   defaults.
2. In `settingsload.py`, add:
   - `resolve_root(mode, *, root_option, installed_root, app_layer, cli_layer, env, system) -> RootChoice`.
     `installed_root` is the root the running gupkg is installed in, or `None`. System mode uses it only when it is
     admin-only, user mode only when it is not.
     `RootChoice` holds `path` and `source`, for example `"built-in default"`, `"--root"` or `"$App\\settings.toml"`;
   - `load_mode_settings(mode, root, *, app_dir, settings_path, system, env, read_text) -> LoadedSettings`.

   Both are pure apart from reading the files through `read_text` and asking `system.directory_untrusted_reason`.
3. Remove `LAYER_SYSTEM` and `LAYER_USER`, and use one `LAYER_ROOT`. Update `manager_policy.py`: no `elevated`
   parameter, and no user-layer special case. The run's layers are already the right ones.
4. Change the existing callers **only as much as needed to keep the suite green**: `cli.py`, `modes.py` and
   `gupkg_ui/settings.py`. They may keep calling a thin adapter until T05. Mark the adapter with
   `# Removed in issue 035 T05.`
5. Settings files the CLI writes (`settings init|set`, `registry add|remove`, `settingsedit.py`) write schema 4. They
   refuse a `[roots]` key in a root layer.

## New tests (each must fail on `main`, then pass)

- [ ] **The fallback chain, one test per row and mode** (9 tests: 4 system, 5 user). Each test sets the row and every row below it, and
      checks that the row wins and that `source` names it.
- [ ] `[roots]` in `<root>\.gupkg\settings.toml` is a `SettingsError` naming the file.
- [ ] `--settings` in system mode is a `SettingsError`.
- [ ] User mode with an admin-only `installed_root` (the system gupkg run by a standard user) skips row 3 and gets
      the next row.
- [ ] A `schema = 3` file is a `SettingsError` with the message above.
- [ ] `--root` in system mode, pointing at a folder `FakeSystem` reports as user-writable: `SettingsError`.
- [ ] A user run never reads the system root's `settings.toml`. Pass a `read_text` that records every path it is asked
      for, and assert that no recorded path is under the system root.

## Done when

- [ ] `git grep -n "default_mode\|integration.system\|integration.user\|LAYER_SYSTEM\|LAYER_USER"` matches only lines
      marked `# Removed in issue 035 T05.`, or nothing.
- [ ] The full suite passes.

## Do not

- Remove `--mode` or `select_mode`. That is T05.
- Change the catalog. That is T06.
