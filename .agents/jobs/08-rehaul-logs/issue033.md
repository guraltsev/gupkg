# Issue 033 log: bundled mode, package launchers, bootstrap

Branch `rehaul/issue033` from main 2828520. Baseline `scripts\test.cmd` equivalent (main's `.venv` python, run from the worktree):
3760 passed, 0 failed, 0 skipped.

## Files expected to change

- New core launcher module `src/gupkg/launchers/` (`__init__.py`, `package-launcher.cmd`); deleted `src/gupkg_migrate/launchers.py`,
  `src/package-tui.cmd`, `tests/test_package_tui_launcher.py`. Tree wrappers `src/gupkg.cmd`, `src/gupkg-tui.cmd`, new `src/gupkg-manager.cmd`
  lose their `PATH` search.
- `[root_forwarder]`: `model/manifest.py`, `engine/effects.py` (effect kind `forwarder`), `pkgcheck.py`.
- check-updates writes launchers in every root and carries only the `gupkg` folder: `engine/checkupdates.py`, `engine/executor.py`.
- `authoring.py` + `cli.py` (`pkg new`, `pkg bundle`, `pkg export` write/include launchers); `gupkg_migrate/package.py`; `gupkg_registry/*`.
- gupkg's own package: `src/pkg.toml` (format 2 tracker), `src/pkg.local/populate.py`, `src/pkg.local/track.py` (replace `check_update.py`,
  `unpack_app.py`), `integrity.py`, `tools/build_standalone.py`, `tools/release.py`, `selfmgmt/status.py`.
- `gupkg-bootstrap.ps1`, `gupkg-bootstrap.cmd`, README "Installing gupkg", `_docs/troubleshooting.md`, `tests/manual_smoke.md`.

## Decisions taken (design does not say, or the issue text is stale)

- Layout fixed by 016/029/030 code: a version directory holds `gupkg\` = the bundled tree (`gupkg.cmd`, `gupkg-tui.cmd`, `gupkg-manager.cmd`,
  `gupkg\` Python package with `python\`, `gupkg_ui\`, ... and `MANIFEST.sha256`). The manifest lists paths relative to the version directory
  (`gupkg/...`); `MANIFEST.sha256` verification is restricted to the tree (`gupkg/` entries), because `pkg.toml`, the launchers and `pkg.local` of a
  version directory written by `check-updates` are not byte-identical to the ZIP's.
- Owner clarifications (progress 2026-10-24): `check-updates` writes `install.cmd` and `gupkg-tui.cmd` into every new version directory of every
  root (not only bundled roots); a `_check_updates` folder carries only `check-updates.cmd`, never `install.cmd`.
- The root forwarder calls the target by a path relative to itself (`%~dp0..\gupkg\current\App\gupkg.cmd`).
- Elevated check: PowerShell script travels in the launcher (spike S1), now recursive and file-aware; elevation is detected from the integrity
  level of `whoami /groups` (High or System = elevated; unknown = refuse).
- 1.x guard: the 1.0 tracker refuses a release of a major version greater than the running gupkg's (names the bootstrap).

## Acceptance criteria and coverage

| Criterion | Coverage |
| --- | --- |
| Bundled install leaves no gupkg behind (+ registry log, nothing under AppData) | tests/test_bundled_mode.py::test_a_bundled_install_leaves_no_gupkg_behind_and_touches_nothing_else; manual 033-2 |
| Bundled install manages itself | test_a_bundled_install_manages_itself_across_a_check_and_an_update, test_purging_a_bundled_install_leaves_only_the_roots_data_folder; manual 033-2 |
| Bootstrap | tests/test_bootstrap_script.py, tests/test_self_release_update.py (install side); manual 033-3 |
| Installed gupkg takes over | test_package_launcher.py forwarder/--no-system-gupkg tests; manual 033-5 |
| Too-old fallback | test_package_launcher.py::test_exit_code_6_*, test_cli_machine_interface (exit 6); manual 033-5 |
| No PATH search | test_package_launcher.py::test_a_gupkg_on_path_is_never_used, ::test_a_gupkg_exe_in_the_current_directory_is_never_used |
| Elevated refusal | test_package_launcher.py (simulated elevation: refusal, PowerShell blocked, unknown level; real elevation: needs_admin tests); manual 033-1 |
| Squatted system root | test_bootstrap_script.py::test_a_root_that_a_standard_user_created_is_not_admin_only; manual 033-4 |
| Mark of the Web | tests/test_distribution_copy.py (025), docs/troubleshooting; manual 033-7 |
| Self-update | tests/test_self_release_update.py; manual 033-6 |
| Uninstalling gupkg | test_the_forwarder_is_recorded_in_the_install_record_and_removed_by_uninstall; launcher fallback in test_package_launcher.py; manual 033-5 |
| System install of a distribution | test_a_system_install_of_a_distribution_runs_nothing_from_the_source_folder, test_engine_distribution.py; manual 033-8 |
| Recoverable bundled installs | test_a_populate_script_that_copies_a_shipped_folder_makes_app_recoverable |
| cleanup --bundled | test_cleanup_bundled_* and test_plain_cleanup_never_removes_a_bundled_copy |

## Unclear in the issue

- Item 12 "never removes gupkg's own bundled copies": the engine already enforces it; tested.
- "Settings" in `<root>\.gupkg\` at bootstrap: no default `settings.toml` is written (gupkg reads defaults when absent).
