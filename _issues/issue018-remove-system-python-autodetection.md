# Issue 018: Remove system Python autodetection

Date: 2026-10-22
Priority: High
Change type: Launcher (`gupkg.cmd`), `bootstrap.py`, `elevation.py`,
dev scripts; no package-format changes

## Objective

gupkg runs on exactly one interpreter: `gupkg\python\python.exe` inside its
own version directory, started as `-I -s -B -S`. Nothing looks for another
Python. There are no `PATH` probes, no `py` launcher, no override variables
or files, and no "ordinary versus elevated" difference in interpreter
choice. The dev tree follows the same rule: its scripts use one explicit
interpreter and never probe.

This issue implements decision 1 of issue 016. Runtime downloads and pip
are covered by issues 016 and 019.

## Current state

### Shipped code

| Where | What it does |
|---|---|
| `src/gupkg/gupkg.cmd:60-64` | Ordinary launch: honors `GUPKG_PYTHON`, then a `gupkg.python` file beside the launcher. |
| `src/gupkg/gupkg.cmd:66-72` | Uses the bundled runtime only if a version probe passes, and otherwise falls through. |
| `src/gupkg/gupkg.cmd:74-88` | `call python -c ...`, then `call py -3 -c ...`: the first Python 3.11+ on `PATH` or registered with the `py` launcher wins. |
| `src/gupkg/gupkg.cmd:14-18` | Ordinary launch prepends the package parent to the caller's `PYTHONPATH`. That only works because ordinary launches do not use `-I`. |
| `src/gupkg/gupkg.cmd:93-102` | Two run labels (`:run`, `:run_embedded`) because the interpreter might not be the bundled one. |
| `src/gupkg/bootstrap.py:60, 79-85` | `--embedded` flag and the `GUPKG_BUNDLED_RUNTIME` marker tell the code whether it is on the bundled runtime. |
| `src/gupkg/elevation.py:127-168` (`enforce_elevated_runtime`) | Checks `sys.executable` is the bundled `python.exe` and `-I -s` is active, **only for elevated runs**. |

As a result, a non-elevated `gupkg` can run on any Python the user has
installed, with that Python's own site-packages, `PYTHONPATH` and user site.
Behavior differs between machines and between elevated and non-elevated
runs.

### Dev tree

| Where | What it does |
|---|---|
| `scripts/_python.cmd:4-19` | Uses `.venv`, then the committed runtime, then `py -3`, then `python`. |
| `scripts/build-standalone.cmd:8` | Overrides the choice with the committed runtime, because that runtime has pip (see issue 019). |
| `tests/test_elevated_launcher.py` | Asserts the elevated branch ignores `GUPKG_PYTHON` / `gupkg.python` / `PYTHONPATH`, which implies the ordinary branch honors them. |

## Design

### 1. One launch path in `gupkg.cmd`

```bat
@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "GUPKG_PY=%~dp0python\python.exe"
if not exist "%GUPKG_PY%" (
    >&2 echo [gupkg] The bundled Python is missing: %GUPKG_PY%
    >&2 echo [gupkg] Reinstall gupkg from a release ZIP.
    exit /b 1
)
rem argument normalization loop (unchanged)
"%GUPKG_PY%" -I -s -B -S "%~dp0bootstrap.py" %GUPKG_FORWARD_ARGS%
exit /b %ERRORLEVEL%
```

- Delete `GUPKG_PYTHON`, `GUPKG_PYTHON_FILE`, `GUPKG_PYTHON_OPTIONS`,
  `GUPKG_EMBEDDED`, `GUPKG_BUNDLED_RUNTIME`, the version probe, the `python`
  and `py` probes, and the `PYTHONPATH` handling.
- Keep the `fltmc` elevation probe only if `bootstrap.py` still needs
  `--elevated`. Prefer letting `elevated_mode()` detect elevation itself
  (`IsUserAnAdmin`), and drop the flag.
- `-B` for every run: the version directory is immutable, so no
  `__pycache__` is written into it.

### 2. `bootstrap.py` sets its own import path

`-I` ignores `PYTHONPATH` and the script directory, so `bootstrap.py` inserts
the package parent (`Path(__file__).resolve().parents[1]`) at
`sys.path[0]` before importing `gupkg`. This is what the elevated path
already relies on. Remove `--embedded` and the `GUPKG_BUNDLED_RUNTIME`
marker. Everything is the bundled runtime now.

### 3. Enforce the interpreter on every run

Rename `enforce_elevated_runtime` to `enforce_bundled_runtime` and call it on
every start, before any other `gupkg` import:

- `sys.executable` must be `<package>\python\python.exe`, and
  `sys.flags.isolated` must be set. Otherwise exit with
  `Start gupkg through gupkg.cmd or gupkg.exe; it runs only on its bundled Python.`
- The admin-only directory checks stay limited to elevated runs.

This also catches `python -m gupkg` from a checkout with a random
interpreter. Developers use `scripts\run.cmd` instead (section 4).

### 4. Dev tree: one explicit interpreter

- `scripts/_python.cmd`: use `%REPO_ROOT%\.venv\Scripts\python.exe` only.
  If it is missing, print
  `Create it: <python> -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt`
  and exit 9009. Remove the committed-runtime, `py -3` and `python`
  fallbacks.
- `requirements-dev.txt` (new): pytest plus the runtime lock's packages, so
  tests import the same versions the bundle ships.
- `scripts\run.cmd` (new): runs `src\gupkg\gupkg.cmd` for manual testing.
  It goes through the bundled runtime like a user would.
- `scripts/build-standalone.cmd`: use `%PY%` from `_python.cmd`. Line 8
  goes away together with issue 019.

Tests that start gupkg as a subprocess use `src\gupkg\gupkg.cmd`, never
`sys.executable -m gupkg`.

### 5. Documentation

README "Installing gupkg" (the "uses a system Python 3.11+ when one exists"
sentence and the "Requires ... Python 3.11 or newer" line),
`_docs/getting_started.md`, `_docs/troubleshooting.md` and
`_docs/development_guide.md` describe one interpreter and the `.venv` dev
setup.

## Tests

Behavior to protect (`tests/test_launcher.py`, replacing the "ignores
overrides in elevated mode" cases in `tests/test_elevated_launcher.py`):

- With a fake bundled `python.exe` that records its arguments, a
  **non-elevated** launch runs it with `-I -s -B -S`, even when `GUPKG_PYTHON`
  is set, `gupkg.python` exists, `PYTHONPATH` is set, and a decoy
  `python.exe` and `py.exe` are first on `PATH`. The decoys must not run.
- Without `python\python.exe`, `gupkg.cmd` exits 1 with the reinstall
  message and starts no process.
- Running `bootstrap.py` with a different interpreter, or without `-I`,
  exits with the "runs only on its bundled Python" message.
- `scripts\_python.cmd` without `.venv` exits 9009 even when `python` is on
  `PATH`.

## Acceptance checklist

- [ ] `grep` over `src/` and `scripts/` finds no `py -3`, `call python`,
      `GUPKG_PYTHON`, `gupkg.python`, `GUPKG_EMBEDDED`,
      `GUPKG_BUNDLED_RUNTIME` or `--embedded`.
- [ ] On a machine with a system Python 3.13 on `PATH`, `gupkg --version`
      reports `sys.executable` (in `manager self status`) as the bundled one.
- [ ] Elevated and non-elevated runs start the same interpreter with the
      same flags.
- [ ] `scripts\test.cmd` passes from a fresh `.venv`.
- [ ] Docs updated as in section 5.

## Integration with the 1.0 rehaul

Added 2026-10-22. See `_docs/rehaul_design.md` §20 and epic issue 020.

This issue is **stage 0** of the rehaul. Its decisions stand and apply to
every entry point the rehaul adds:

- **`gupkg-tui` and `gupkg-manager`** (issue 030, decision D34) each get
  the same single launch path, `gupkg-tui.cmd` and `gupkg-manager.cmd`,
  which run `python\python.exe -I -s -B -S` relative to themselves, and call
  `enforce_bundled_runtime` on start.
- **Both UI programs start the `gupkg.cmd` next to them in the same gupkg
  tree** (`$App\gupkg.cmd` for an installed gupkg,
  `<version>\gupkg\gupkg.cmd` for a bundled one) and never search `PATH`.
  This is the same rule as the interpreter rule here, applied to the CLI
  (§10.4 of the design).
- **Elevated relaunch.** It relaunches the same bundled launcher (issues 025
  and 029), so elevated and non-elevated runs start one interpreter.
- **`--sandbox`** (issue 021) runs through the same launcher. A sandbox does
  not change the interpreter rule.
- **Tests.** The rehaul's CLI contract tests (issue 029) start `gupkg.cmd`,
  as this issue requires. Unit and engine tests run in `.venv` against
  `FakeSystem`.
- **Bundled mode (issue 033).** A bundled gupkg can sit inside any
  program's version directory (`<version>\gupkg\`). Because the launcher
  resolves `%~dp0python\python.exe` relative to itself, the rule here works
  unchanged wherever the bundle is.
- **Package launchers follow the same spirit.** `install.cmd` and
  `gupkg-tui.cmd` find a gupkg only through the root forwarder
  `%~dp0..\..\.gupkg\gupkg.cmd` or the bundled `%~dp0gupkg`. They never use
  `PATH`, the registry, AppData or ProgramData (decision D16). They use the
  installed gupkg behind the root forwarder unless `--no-system-gupkg=true`
  is given, and otherwise the bundled copy, with no version comparison
  (decision D32). Elevated launchers refuse unless everything they will run
  is admin-only, checked recursively: the root, the package and version
  directories, `<root>\.gupkg\`, the forwarder, and the whole gupkg and
  Python tree that will start (threat model T1, decisions D19, D30). The
  PATH search in today's `gupkg-tui.cmd` is removed by issue 033.
