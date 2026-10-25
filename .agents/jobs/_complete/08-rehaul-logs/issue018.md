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

## Review round 1

- bootstrap.py now runs the checks before touching sys.path; elevation.py preloads ctypes and
  wintypes at import (loaded by file path), so no check import can resolve from the package
  directory. __main__.py delegates to bootstrap.main (same order).
- is_elevated() fails closed on Windows: IsUserAnAdmin or TokenElevation true, or either signal
  failing to read, counts as elevated. GUPKG_ELEVATED=1 stays stricter-only.
- test_launcher.py parametrisations renamed (the launcher has no elevated branch); the elevated
  behaviour is covered at bootstrap level in test_elevated_runtime.py.
- _docs/development_guide.md: "Development environment" section. getting_started and
  troubleshooting have no relevant text.
- NOT done, routed to later issues: the ACL check is not recursive over the runtime tree (D30).

## Review round 2

- Probed on a copy of the real bundled layout: with the old _pth (".", "..", "import site") a planted
  sitecustomize in python\ and a .pth in site-packages ran before bootstrap. An "import site" line in the
  _pth overrides -S, so both changes are needed: launcher -I -s -B -S, and a _pth of only
  python312.zip, ".", Lib/site-packages (src/gupkg/python/python312._pth, tools/fetch_runtime.write_pth).
- enforce_bundled_runtime also requires sys.flags.no_site, and in an elevated run checks python.exe and
  the python*._pth file as well as their folders.
- The old planted-module test is replaced by real-layout tests in test_launcher.py; the sys.path-order test
  fails on the old order. ImportError while loading elevation.py prints a reinstall message, exit 1.
- Imports of the bundled runtime verified under -S (textual, rich, markdown_it, ssl, sqlite3, ...).
- Docs and rehaul_design flag text updated to -I -s -B -S. Issue 018's own text still says -I -s -B.
