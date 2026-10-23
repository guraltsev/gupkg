# Issue 019 worker log

Branch: rehaul/issue019 (from main 3760ff7).
Baseline: 296 passed, 0 failed, 0 skipped (run with the main checkout's `.venv` python; the worktree has no `.venv`).
That `.venv` was made by uv and has no pip. The pip-using tests find one through `sys.base_prefix`
(the `build_python` fixture) and fail, not skip, when none exists.

## What issue 016 already delivered (not redone)

`_ensure_pip`, `dependencies.py`, `locked_deps.py` (`_EXTRA_ROOTS`) and the bootstrap's pip install are gone;
the lock lives in `tools/gupkg-deps.lock`; `tools/build_standalone.py` already installed wheels with the
build interpreter's pip. 019 therefore does: lock content (pip out, tomli_w in), the committed runtime,
the tool changes that made pip a runtime dependency of the tools, the build checks and probe, scripts, docs.

## Files changed

tools/fetch_runtime.py, tools/update_deps.py, tools/build_standalone.py, tools/gupkg-deps.lock,
scripts/build-standalone.cmd, fetch-runtime.cmd, release.cmd, src/gupkg/python/Lib/site-packages (pip removed,
tomli_w added, RECORD files tidied), tests/conftest.py (`write_runtime_zip`, `runtime_zip_factory`),
tests/test_release_bundle.py (stub python.exe replaced by the real committed one, because the probe runs it),
tests/test_update_deps_tool.py, new tests/test_runtime_without_pip.py, tests/manual_smoke.md, docs.

## Decisions

- Probe imports now: every locked package by import name (from each dist's RECORD), then `gupkg.cli`,
  `gupkg.manager`, `gupkg.tui`, `gupkg.manager_tui` (today's UI code lives in the gupkg package). The
  separate programs of issues 029/030 (`gupkg_ui`, `gupkg_tui`, `gupkg_manager`) and the migration tool
  do not exist yet; the probe imports `gupkg_ui`, `gupkg_tui`, `gupkg_manager` automatically when a package
  directory of that name exists in the version directory. The migration tool's module name is not settled
  (issue 023), so it is not listed. With `--skip-deps` only `gupkg.cli` (needs no third-party package) is
  imported, and the pip/ensurepip absence is still asserted.
- Installer check (pip, setuptools, `_distutils_hack`, ensurepip, also `*.dist-info`) runs on the staged runtime
  even with `--skip-deps`, so a stale runtime archive cannot bring pip back. It also looks at python*.zip.
- `update_deps.py` and `fetch_runtime.py` now run the build machine's pip (`sys.executable`, or `--python`),
  cross-targeted (`--platform win_amd64 ... --abi cp312`), never the runtime's. The staged probe uses the
  runtime python only to import. Probe now puts the staging directory first on sys.path (it was appended, so an
  older live copy could shadow it).
- Offline builds: `--find-links DIR` (adds `--no-index --find-links`; hashes still required) and
  `build_bundle(find_links=, python=, probe_modules=)`.
- The committed runtime was refreshed by running `tools/update_deps.py --repair --python <old committed python>`
  once (the only pip around, uv .venv has none) after editing the lock; wheels came from PyPI with hashes.
  The installs now use `--no-compile`, so the RECORD files lose their `__pycache__` lines.
- tomli_w 1.2.0 (same version pip vendored); hash from PyPI's JSON for the wheel.

## Acceptance criteria -> coverage

See the final report.

## Follow-ups for later issues

Issues 023 (migration tool), 029 and 030 (new UI packages) must add their modules to `PROBE_MODULES` /
`OPTIONAL_PROBE_PACKAGES` in `tools/build_standalone.py`.
