# Issue 018 worker log

Branch: rehaul/issue018 (from main 3760ff7). Baseline: 296 passed, 0 failed, 0 skipped
(run with the main checkout's .venv interpreter, as there is no .venv here).

## Already delivered by issue 016 (not redone)

- gupkg.cmd already ran only `python\python.exe -I -s -B`, with no GUPKG_PYTHON, gupkg.python,
  PATH, py or version-probe logic; no `--embedded`, `GUPKG_EMBEDDED` or `GUPKG_BUNDLED_RUNTIME`.
- tests/test_launcher.py already covered the override-ignoring launch and the missing-runtime exit.
- bootstrap.py already inserted the package parent into sys.path.

## Remaining scope, done here

- gupkg.cmd: dropped the fltmc probe, `--elevated` and `GUPKG_FORCE_ELEVATED`; one launch line.
- elevation.py: `enforce_elevated_runtime` -> `enforce_bundled_runtime(package, elevated=False)`;
  interpreter and `sys.flags.isolated` are checked on every run, ACL checks only when elevated.
- bootstrap.py and __main__.py call it first on every start (elevation from `elevated_mode()`).
- scripts/_python.cmd: `.venv` only, exit 9009 with the creation command; scripts/run.cmd;
  requirements-dev.txt; test.cmd message. build-standalone.cmd line 8 left for issue 019.
- tests: test_launcher.py, test_elevated_runtime.py, test_runtime_cli.py (now starts gupkg.cmd),
  new test_dev_environment.py. Docs: README, _docs/operations.md, tests/manual_smoke.md,
  docs/elevated-runtime-design.md.

## Decisions

- Elevation detection moved fully into Python (IsUserAnAdmin via `elevated_mode()`), as the issue prefers.
  The launcher's fltmc probe was a second, redundant signal; Python already used the token.
- Isolation is checked with `sys.flags.isolated` (set only by -I), which implies no user site.
- requirements-dev.txt repeats the lock's package versions without hashes (pip's hash mode would
  reject pytest); a test checks the two stay equal, ignoring pip (issue 019 may drop it).
