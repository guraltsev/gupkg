# Issue 021 worker log

Branch: rehaul/issue021 (from 513cdeb). Baseline on main: 355 passed, 0 failed, 0 skipped.
After: 408 passed, 0 failed, 0 skipped (`-m windows_integration`: 35 selected, all pass).

## Files changed

- New `src/gupkg/system/`: `port.py` (System protocol, value types, `is_elevated_run`,
  `require_admin_only`, `run_checked`), `windows.py` (`WindowsSystem`; absorbs the old
  `windows.py`, the COM code of `shell_link.py`, the junction code), `fake.py`
  (`FakeSystem`, sandbox persistence).
- New `src/gupkg/selfmgmt/elevation.py` (moved from `elevation.py`; stays self-contained
  because `bootstrap.py` loads it by path before the package is importable).
- Deleted: `windows.py`, `shell_link.py`, `elevation.py` (moved), the duplicate
  `is_current_user_admin`.
- Callers now take a `System`: `layout.py`, `components.py`, `gupkg.py`, `origin.py`,
  `updates.py`, `registry.py`, `github_releases.py`, `downloads.py`, `commands.py`,
  `manager.py`, `distribution.py`, `cli.py` (`--sandbox DIR`, `main(argv, *, system=)`),
  `tui.py`, `manager_tui.py`, `core.py` (`Scope` lives in `system/port.py`;
  `enable_virtual_terminal` from `system/windows.py`).
- `src/pkg.local/check_update.py` (gupkg's own update hook) reads RELEASES.md through the
  hook context's new `fetch_text`, instead of `urlopen`.
- `src/gupkg/shortcuts_to_gupkg_toml.py`: one import changed (the COM reader moved).
- Tests: new `tests/test_system_contract.py`, `tests/test_fake_system.py`; `tests/conftest.py`
  gains a `system` fixture; `pytest.ini` marker text; rewritten onto FakeSystem:
  `test_gupkg_cli_behaviors`, `test_update_lifecycle`, `test_registry_git`,
  `test_registry_download`, `test_registry_cache_trust`, `test_manager_config_location`,
  `test_download_progress`, `test_github_releases_update`, `test_self_release_update`,
  `test_update_hooks`, `test_issue012_distribution_registry`, `test_helper_scripts`,
  `test_tui`, `test_manager_tui`, `test_gupkg_manager`, `test_package_root_resolution`,
  `test_shell_link`, `test_windows`, `test_elevated_runtime`, `test_color_output`.
- Docs: `README.md` (`--sandbox`), `src/gupkg/README.md` (module list),
  `docs/elevated-runtime-design.md` (module name), `tests/manual_smoke.md` (section for 021).

## Acceptance criteria and coverage

See the final report.

## Decisions taken inside the issue's room

- `System` gains `scope_folders(scope)` (Start Menu `opt` and `bin` folders of a scope), so
  sandbox mode can keep shortcuts and wrappers off the real machine.
- `directory_is_admin_only(path) -> bool` plus `directory_untrusted_reason(path)`, so
  elevated-run refusals keep naming the offending principal and the repair command.
- `run(argv, cwd, env, on_line)` also takes `input_text` and `timeout` and returns a
  `ProcessResult`; script paths (`.ps1`, `.cmd`) are expanded to their interpreter inside
  `WindowsSystem`, so no caller names a shell; there is no `powershell` literal outside
  `system/windows.py`.
- `fetch(url, headers, dest, on_progress)`; progress rendering stays in `downloads.py`.
- The shell-link COM code is merged into `system/windows.py` (the acceptance `rg` allows
  only that file and `selfmgmt/`).
- `FakeSystem` links are real links (junctions) below the temporary root, reusing
  `WindowsLinks`, because the installed tree must stay usable (`current\App\...`).
- Hook context (`updates.check_update`) now carries `fetch_text`.
- `is_elevated()` is the single, fail-closed implementation (was `elevation.is_elevated`;
  the fail-open `windows.is_current_user_admin` is gone). A failed detection now counts as
  elevated everywhere (scope auto-selection, `needs_elevation`).

## Unclear points

- Acceptance 1 says `rg ... src/gupkg` matches only `system/windows.py` and `selfmgmt/`;
  read as excluding the vendored `src/gupkg/python/` tree.
- The issue lists no uninstall command in this codebase; the D38 behaviour test therefore
  covers install and update.

## Review round 1

- Sandbox processes: allow-list is git plus 7-Zip; no blanket delegate (network keeps the
  real delegate). The issue names no sandbox allow-list, so package origin/hook scripts
  fail in a sandbox: open question for the controller.
- Roots are not redirected (the issue does not ask for it); documented in README and smoke.
- check_update.py: a missing fetch_text gives a clear RuntimeError.
- Controller note: port.py module docstring now lists the protocol additions
  (scope_folders, directory_untrusted_reason, run's input_text/timeout, script expansion
  inside WindowsSystem).

## Review round 2

- Sandbox process allow-list is by identity: `git` resolved on PATH (never the working
  directory) and the exact `find_7z()` path; suffixes other than none/.exe are refused;
  a path must equal the resolved executable; allowed programs run from the resolved path
  with `timeout` and `on_line` passed through.
- Note for issues 026/028 (controller to carry): design section 16 has
  `gupkg pkg check --install` running a sandboxed install. With this allow-list, any
  package-local program, installer or script raises `UnexpectedCall` in a sandbox. Issue
  028 must route every external process through `System.run`, and let the sandbox install
  declare per-package doubles or an explicit opt-in allow-list, or treat `UnexpectedCall`
  as a clear "needs a real run" result instead of a crash.
