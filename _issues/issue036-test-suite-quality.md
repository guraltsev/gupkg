# Issue 036: The test suite is too big, slow, repetitive and disorganised, and still misses dangerous workflows

Date: 2026-10-30
Priority: High
Policy: [_docs/tests.md](../_docs/tests.md), [_docs/tests_hygiene.md](../_docs/tests_hygiene.md) (written for this issue)
Work: job 10 (`.agents/jobs/10-test-hygiene-controller.md`)

> **Status:** Open. This file describes the failures and the owner's decisions. How they are fixed is job 10.

## 1. Owner's requirements (2026-10-30)

1. **Well-designed tests, not a powerful machine.** The suite must be fast because its tests are good. Parallel runs are
   allowed as an extra, never as the fix.
2. **A well-defined tier system.** Running the whole suite after every edit (`AGENTS.md` rule 3 today) is unacceptable.
3. **Less token pressure.** The tests must be small and organised enough that a new implementation can find, read and
   respect the relevant ones without reading hundreds of thousands of tokens.
4. **Temporary tests are allowed during long work,** but linked to an issue or job. When the work closes, they are
   partly promoted to good permanent tests and the rest retired.
5. **The test rules are general.** They should serve as a starting point for a test policy in other projects too.
6. **No loss of protection.** Every behaviour protected today stays protected, possibly by a cheaper test.

## 2. Measurements

All figures were taken on 2026-10-30, on `main` at `0e54cbc` plus uncommitted work, with one serial run of the full suite.

| What | Value |
| --- | --- |
| Tests collected | 3,770 (1,763 test functions; the rest is parametrisation) in 138 test files |
| Result | 3,757 passed, 13 skipped |
| Wall time | 7 min 14 s, serial; a second pytest run overlapped the last 1.5 minutes |
| Test code | About 438k tokens (148 files, including helpers), against 405k tokens of source: ratio 1.08 |
| Share of docstrings in the test text | 21% |
| Test names | Median 61 characters; 197 are longer than 80 |
| Growth | Test code grew from 392k characters at v0.18.0 (2026-10-22) to 1,672k at v1.0.0 (2026-10-24) |
| Removal | Issue 035 removed about 100k characters of source but only about 37k of tests |

**Where the time goes:**

| Slice | Tests | Serial time |
| --- | --- | --- |
| Each under 0.1 s | 3,157 (84%) | 55 s (13%) |
| Slowest 200 | 200 | 302 s (71%) |
| Slowest 10 | 10 | 66 s (16%) |

## 3. Failures

Findings F3–F12 come from three read-only reviews (engine and catalog; CLI, UI and manager; system, network, packaging
and manifest), which covered about 3,140 of the 3,770 cases. Each reviewer estimated that **25–30% of the lines in its
scope** can go without losing a user-observable check, and more of the cases. These are estimates; job 10 checks them case
by case.

### F1. There are no tiers, and the rules require the full suite after every edit

The only marker is `windows_integration`, and nothing selects by it. `scripts\test.cmd` always runs everything. `AGENTS.md`
rule 3 requires the full suite "after every source change", so every edit waits more than 7 minutes.

### F2. A few tests waste most of the time

- **Real retry delays.** Four tests take 7.0 s each (28 s in total), because the HTTP client's retry delay sleeps for real:
  1 + 2 + 4 s, shown by a profile. The client already accepts an injected `sleep` (`src/gupkg/services/http.py:209`), but
  these tests do not pass one:
  - `test_registry_tool.py`:
    - `test_the_exit_status_is_one_when_a_package_fails_and_two_without_a_registry_file`;
    - `test_a_staging_folder_that_cannot_be_removed_is_a_warning_not_a_failure`;
    - `test_a_run_removes_the_staging_folders_an_interrupted_run_left_but_nothing_else`;
  - `test_record_http_fixtures.py`:
    - `test_a_failing_run_leaves_the_case_file_untouched`.
- **`test_registry_validate`, 50 s for 33 tests.** Each test copies the whole 79-package registry snapshot and validates
  all of it, to check one planted fault.
- **`test_regex_runner`, 41 s for 197 cases.** Each case starts its own worker process.
- Other heavy modules: `test_system_contract` (41 s), `test_runtime_without_pip` (23 s), `test_ui_screens` (22 s),
  `test_manager_real_cli` (21 s) and `test_release_bundle` (18 s).

### F3. Tests are filed by issue, not by feature

- File names like `*_cases`, `*_hardening`, `*_security`, `*_guards`, `*_limits`, and three files about scope
  (`scope`, `scope_model`, `scope_isolation`).
- 135 references to issues, decisions, tasks, review rounds or 0.17 in 61 files.
- Each agent added a new file instead of finding the existing home, so the same behaviour was tested again.

### F4. The same rule is tested at every layer

| Rule | Places tested |
| --- | --- |
| The checksum policy table | 4 files |
| A malformed digest is refused | 5 places |
| The events file | 5 places |
| Redirected and `--json` output | 5 places |
| The shell-unsafe argument rule | 5 places |
| Argument forwarding through the launchers | About 70 real `cmd` runs |
| Wrong-interpreter refusal | 4 levels |
| A tree checked against `MANIFEST.sha256` | 5 files |
| Bad `PATH` entry shapes | 4 layers |
| "The token decides the mode" | 3 times |
| The engine state table | End to end 2–3 times |

### F5. History tests

These check that a removed thing stays removed: 0.17 commands and options, the `relaunch` key, removed manifest keys,
the root forwarder, the old receipts place. One test of the general rule covers all of them.

### F6. Tests of internals, and mutation-testing goals

- `test_unpack_guards` exists "so removing any single [guard] fails a test". It calls the private `_FolderWatch` and spies
  on `extract_archive`.
- Tests count calls by monkeypatching (`Catalog.installed_effects`, `_ProcessJob.close`).
- Three tests in `test_checksum_fetch` parse the import closure of `src/`.
- Several tests patch private names (`lifecycle._dispatch`, `_admin_signal`, `tomli_w._writer`).

### F7. Many cases of the same rule

- 29 "one digit differs" cases that prove string `==`;
- about 83 hostile version names, one case each;
- 34 hostile installer arguments, each running a full install, without `id=`;
- 25 + 7 events-file names;
- 29 Python binding forms of `track`;
- 50 launcher argument-forwarding runs.

### F8. Copied setup and tangled modules

- The `world` fixture is copied about 26 times, mostly as `return World(tmp_path)`.
- 42 files import `pin_no_ambient_elevation`, which repeats the autouse fixture `no_ambient_elevation_marker` of
  `tests/conftest.py`.
- Test modules import from each other: `engine_unowned_files` from `engine_name_conflicts`, and `scope_isolation` from
  `cli_tools` and `ui_screens`.
- Helpers exist in several copies: `registry_zip`, `current`, `_Collector`, `Terminal`, and a hash-corruption snippet
  (4 copies).

### F9. Registry data is tested inside gupkg, on stale copies

- `tests/fixtures/registry_100` holds 79 packages; the live registry has 95.
- `tests/fixtures/registry_patterns.json` holds the patterns of the 0.17 hooks, which issue 035 removed.
- Four checks of `test_registry_100` repeat the registry's own `_tests/test_registry.py`.
- The registry's CI runs four gupkg modules against its live data (`test_track_providers`, `test_registry_expressible`,
  `test_registry_trackers`, `test_registry_semantics`), while gupkg runs the same modules on its stale copy.

### F10. Tests of unused code

`gupkg.model.manager_policy.decide_targets` and `command_line` have no caller in `src` (checked). The UI has its own
`decide_targets` in `src/gupkg_ui/plans.py`. Several `test_manager_policy` tests test only that unused code.

### F11. Tests that claim more than they check

- `test_engine_check::test_a_dangling_current_and_a_missing_app_are_reported` never sets up a dangling `current`.
- `test_engine_check_updates::test_the_registrys_tracker_is_copied_in_and_run` does not assert the recorded hash its
  docstring promises.
- `test_engine_version_questions::test_yes_and_the_option_answer_the_broken_versions_question` does not test the
  `--no-input` refusal its docstring describes.
- `test_run_logs::test_a_stale_events_file…` asserts that `OLD-RUN-MARKER` is absent but writes `stale`.
- `test_package_launcher::test_a_gupkg_exe_on_path_is_found_too` would pass for any program.
- `test_pinning::test_a_skipped_download_writes_nothing_into_the_version_directory` cannot fail: the code never receives
  that directory.

### F12. Dangerous workflows are not tested

None of the following is tested, or no test was found:

- the failure of a self-update activation: restoring `current` on a locked file or offline (`AGENTS.md` rule 6).
  `test_junction_replace_while_running` covers only success;
- `check` reporting a dangling `current`;
- the UI activating a version (the version picker is only opened and closed);
- the UI's uninstall with `--purge`;
- the UI's registry sync failing;
- the content of the UI's log view;
- `check --fix --no-input` (not found);
- `gupkg shell` in system mode (not found);
- the cleanup's `keep_days`;
- portable `markers` on update or switch;
- the TLS handshake on a partial chain;
- the real `WM_SETTINGCHANGE` broadcast.

### F13. The old policy contributed

The previous `_docs/tests.md` required:

- a module docstring stating the behaviour covered, the boundaries and what is out of scope;
- a docstring on every test.

It set no limit on duplication, no rule for where a test goes, no tiers and no lifecycle. It also did not say what a
docstring should contain, so docstrings filled up with:

- boilerplate (boundaries and out-of-scope lists);
- history (issue, decision and task numbers);
- claims their tests do not check (F11).

**Detailed docstrings are wanted** (owner, 2026-10-30): they should explain the behaviour well enough to rebuild the
project's documentation from them. The fault is their content, not their length. The new policy (`tests.md` §4.2) says
what a docstring contains. It also requires that a docstring agree with the official documentation: a disagreement means
one of the two is outdated.

## 4. Owner decisions

| # | Decision | Date |
| --- | --- | --- |
| 1 | Test speed comes from test design and tiers. `pytest-xdist` is at most an extra | 2026-10-30 |
| 2 | Agreed fixes: inject `sleep` in the four 7-second tests; small fixtures for `test_registry_validate` | 2026-10-30 |
| 3 | gupkg tests prove gupkg's code with fixtures written for the test, and never copy registry data. Checks of the registry's data live in the registry | 2026-10-30 |
| 4 | The per-package tracker tests, `tests/fixtures/http/` and `tools/record_http_fixtures.py` move to the registry. gupkg keeps provider tests on crafted trackers. The registry commit lands first, because the registry's CI runs those modules from gupkg `main` | 2026-10-30 |
| 5 | Two policy files: `tests.md` (quality, location, tiers) and `tests_hygiene.md` (lifecycle, temporary tests, debt, checks). Both are written generally, with a final section for this project | 2026-10-30 |
| 6 | Speed is a test design concern. No time limit is enforced when tests run | 2026-10-30 |
| 7 | Test docstrings are detailed. They document the behaviour in the product's terms, every claim is asserted, and they agree with the official documentation. A disagreement marks an outdated test or document | 2026-10-30 |
| 8 | Test areas follow the code's real package boundaries: 11 areas (`tests.md` §7) | 2026-10-30 |

**Open for the owner:**

- the wording of the new `AGENTS.md` rule 3 (job 10 T02 proposes it);
- the removal of the unused `manager_policy` code (job 10 T18).

## 5. Done when

- **Tiers:** every test module declares a tier. `scripts\test.cmd` runs `unit` by default. The goal is that the whole
  `unit` tier takes under 30 s, run serially. It is measured and reported, not enforced per test. `AGENTS.md` rule 3 names the tiers.
- **Hygiene checks:** the checks of `tests_hygiene.md` §6 run with the suite, and `tests/hygiene_baseline.txt` is empty.
- **Layout:** every permanent test lives in `tests/<area>/`. No test module imports another, and no fixture data is
  copied from the registry.
- **Failures fixed:** F2–F11 are fixed. For each area, the log of job 10 maps every behaviour protected before to the
  test that protects it after.
- **Gaps:** every gap in F12 has a test that was proven to fail without the code it protects, or the owner has moved it to
  `tests/manual_smoke.md`.
- **Indicators:** the indicators of `tests.md` §2.3 are recorded before and after, and the report states the change.

## 6. Inconsistencies found while measuring (not yet fixed)

- `_docs/tests.md` said tests live in `tests/unit/` and referred to `docs/`; both were wrong. Fixed by the rewrite of
  2026-10-30.
- `src/gupkg/model/manager_policy.py`: `decide_targets` and `command_line` have no caller (F10).
- `test_manager_policy`'s docstring says "the manager program itself does not exist yet (issue 030)".
- `test_cli_lifecycle`'s docstring says the mode prints as `system (auto: elevated)`; the test asserts
  `(the installed system gupkg)`.
- `test_ui_flows` and `test_ui_client` docstrings call `test_manager_cli.py` "the real CLI"; it uses the fake. The real
  one is `test_manager_real_cli.py`.
- `test_ui_client::test_a_good_call_is_ok…`'s docstring says "api 1"; the documents are api 2.
- `test_launcher.py:263` calls the outer `gupkg.cmd` "the target of the shims". The shims now target `python.exe`
  (according to the review; not re-checked).
- `test_registry_expressible`'s docstring names `tests/fixtures/registry_100`. The registry's CI runs the same module
  against the live registry.
- The user-visible error in `src/gupkg/catalog/catalog.py:235` says "a target no longer names a mode": history wording
  shown to users.
- `tests/test_shortcut_admin.py` starts with a UTF-8 byte-order mark; no other test file does.
- The repair key is `i` in the manager's browser and `r` in `gupkg-tui`, for the same question (not checked whether
  intended).
- `test_cli_tools`: a variable named `refused` holds a successful run, in two tests.
