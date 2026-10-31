# T15: Area `self` (runtime, self-update, bootstrap) and area `project` (release build, tools)

Issue 036 F4, F5. Follow the brief §4a. Most of these tests are in the `release` tier. Make them cheaper where you can, but
never at the cost of the real-interpreter checks that `AGENTS.md` rule 6 requires.

## Scope

- **Runtime:** `tests/test_runtime_without_pip.py`, `test_no_runtime_acquisition.py`, `test_runtime_cli.py`,
  `test_runtime_tools.py`.
- **Release:** `test_release_bundle.py`, `test_release_tool.py`, `test_self_release_update.py`, `test_bootstrap_script.py`,
  `test_check_release.py`, `test_bump_version.py`.
- **Maintainer tools:** `test_update_deps_tool.py`, `test_dev_environment.py`, `test_app_info.py`.
- **Shared:** the release-ZIP fixtures in `tests/conftest.py`.

## Target layout

```text
tests/self/
  test_runtime.py          without_pip + no_runtime_acquisition + runtime_cli (the bundled runtime)
  test_self_update.py      self_release_update
  test_bootstrap_script.py
tests/project/
  test_release_bundle.py   release_bundle
  test_release_tool.py     + check_release
  test_update_deps.py
  test_runtime_tools.py
  test_dev_environment.py
  test_bump_version.py
  test_test_report.py      (from T00)
```

What is left of `test_app_info` goes to the area of the code it tests. Name it in your log.

## Findings to act on (review of 2026-10-30; verify each)

- **"No pip" is asserted in about 7 tests** across the runtime files. One owner per entry point (the build, the probe, the
  shipped bundle).
- **A tree checked against `MANIFEST.sha256` (modified / missing / extra)** is parametrised separately in `test_unpack`
  (T13), in `test_release_bundle` (two tests plus planted bytecode), in `test_self_release_update` and in
  `test_check_release`. The checker owns the three cases. Every other entry point gets one case.
- **`test_app_info`:** the "old receipts place" test is history, and another test exercises only `shutil.copytree`.
- **`test_runtime_without_pip`:** 23 s, with several builds. Check whether one session-scoped build can serve several
  tests without making them depend on each other's order.
- **`test_dev_environment`:** checks of the `cmd` scripts against a copied layout. Keep the behaviour tests. Retire the
  "no file probes for a system Python" text search, unless it is the only guard of that rule. In that case, keep it and
  name it a guard.

## Done when

Everything in the brief §4a, plus:

- [ ] The total time of the `release` tier before and after. It must not grow.
- [ ] The self-update tests still start a real interpreter through a junction (`AGENTS.md` rule 6). Name the test that
      does it.
