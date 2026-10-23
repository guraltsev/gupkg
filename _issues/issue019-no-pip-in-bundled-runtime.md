# Issue 019: The bundled Python ships without pip

> **Status (2026-10-22):** Implemented. Merged in 5c922cb. Manual checks still owed: `tests/manual_smoke.md`, "A bundled Python without pip (issue 019)": 019-1 (real `scriptsuild-standalone.cmd` build), 019-2 (clean VM: `-I -m pip` fails; install, update, manager and both TUIs work), 019-3 (`update_deps.py --repair` leaves no change). Issues 023, 029 and 030 must add the migration tool and the new UI packages to `PROBE_MODULES` / `OPTIONAL_PROBE_PACKAGES` in `tools/build_standalone.py`.

Date: 2026-10-22
Priority: High
Change type: Runtime contents, dependency lock, build tooling; removes code
that issue 016 also removes

## Objective

The bundled runtime (`gupkg\python\`) contains CPython and gupkg's runtime
dependencies only. pip is not in the bundle, not in the lock, and nothing at
run time can install packages into it. Packages get into the runtime at
exactly one point: the build, which installs wheels with the **build
machine's** pip into the bundle's `site-packages`.

This follows from issue 016 (no installs at run time). Removing pip makes
that a property of the bundle rather than a code convention.

## Current state

- `src/gupkg/gupkg-deps.lock:6` pins `pip==26.2.1` like a runtime
  dependency. `src/gupkg/locked_deps.py:49` (`_EXTRA_ROOTS = ("pip",)`)
  keeps it in the lock on every `update-deps`.
- The committed runtime contains `Lib/site-packages/pip/` and
  `pip-26.2.1.dist-info/` (450 tracked files).
- Code that runs pip **inside** the bundled runtime:
  - `src/gupkg/bootstrap.py:122-158` `_ensure_pip`: probes
    `python -m pip --version`, and otherwise downloads `get-pip.py`.
  - `src/gupkg/dependencies.py:180-182`: `sys.executable -m pip install`.
  - `src/gupkg/locked_deps.py:350, 407`: `_python(package) -m pip install`
    for `repair-deps` / `update-deps`.
  - `gupkg-bootstrap.ps1:115-129`: downloads the pinned pip wheel, unpacks it
    into the runtime, then runs `python -m pip install -r gupkg-deps.lock`.
- `scripts/build-standalone.cmd:7-8` runs the build with the committed
  runtime *because* it has pip.
- `tools/build_standalone.py:41-62` already does the right thing: it runs
  `sys.executable -m pip install --target <bundle site-packages> --platform
  win_amd64 --implementation cp --python-version 3.12 --abi cp312
  --only-binary=:all: --require-hashes --no-deps`. pip comes from the
  build interpreter, and the bundle only receives wheels.

## Design

### 1. Lock and build

- Remove `pip` from `gupkg-deps.lock` (moved to the dev tree by issue 016)
  and drop `_EXTRA_ROOTS` from the lock tooling (`tools/update_deps.py`).
  The lock lists runtime imports only.
- `tools/build_standalone.py`:
  - Keep the cross-target `pip install --target` from the build interpreter.
  - After installing, **fail the build** if `site-packages` contains `pip`,
    `pip-*.dist-info`, `setuptools`, `_distutils_hack` or `ensurepip`, or if
    `python -c "import pip"` succeeds against the staged tree. This keeps a
    transitive dependency or a stale tree from bringing pip back.
  - Remove `site-packages\bin` (already done) and any `*.dist-info\RECORD`
    entries that point at scripts.
- `scripts/build-standalone.cmd`: use the dev `.venv` (issue 018) and
  delete the "the committed runtime provides pip" override.

### 2. Committed runtime

Delete `src/gupkg/python/Lib/site-packages/pip/` and
`pip-26.2.1.dist-info/` from the repository. Developers who need pip use
`.venv`. `tools/update_deps.py` refreshes the committed runtime's
`site-packages` the same way the build does: target install from `.venv`'s
pip.

### 3. Run-time code

Remove every `-m pip` use in shipped code. Most of it already goes with
issue 016 (`_ensure_pip`, `dependencies.py`, `locked_deps.py`,
`Install-BundledRuntime`). If 019 lands first, delete those paths here and
make issue 016 refer to this change.

### 4. Side effects to check

- **Textual and Rich** do not import pip. **platformdirs** and
  **typing_extensions** have no pip use. Confirm with an import of every
  top-level module in the staged tree under `-I` (the build probe below).
- **Hooks**: issue 016 already forbids hook dependency installs, so no hook
  can rely on pip.
- **Manifest** (issue 016): with pip gone, `MANIFEST.sha256` shrinks by about
  450 entries.

## Build probe

As the last build step, run the staged `python\python.exe -I -s -B -c` with a
script that:

1. imports every top-level package listed in the lock (by import name),
2. asserts `importlib.util.find_spec("pip") is None` and
   `importlib.util.find_spec("ensurepip") is None`,
3. imports `gupkg.cli`.

A failing probe fails the build. This is the "check at build time" form of
the old run-time dependency check.

## Tests

- `build_standalone.py` on a fixture runtime whose wheel set includes pip
  fails with a message naming pip.
- A built bundle (fixture, `--skip-deps` plus fake wheels) contains no `pip`
  directory and passes the probe.
- Shipped-tree scan (test over `src/gupkg/*.py`, `src/pkg.local/*.py`,
  `gupkg-bootstrap.ps1`): no `-m pip`, `import pip`, `get-pip` or
  `pip install`.

## Acceptance checklist

- [ ] `gupkg-deps.lock` has no `pip` line. `tools/update_deps.py` does not
      add it back.
- [ ] `src/gupkg/python/Lib/site-packages` has no `pip*` entries in git.
- [ ] A release ZIP has no `pip` and its build probe passed.
- [ ] `gupkg\python\python.exe -I -m pip` in an installed release fails with
      `No module named pip`.
- [ ] gupkg install, update, manager and both TUIs work from the pip-less
      bundle on a clean VM.
- [ ] `_docs/development_guide.md` says where pip lives (`.venv` only) and
      how the runtime's `site-packages` is refreshed.

## Integration with the 1.0 rehaul

Added 2026-10-22. See `_docs/rehaul_design.md` §20 and epic issue 020.

This issue is **stage 0** of the rehaul. Its decisions stand.

- **New runtime dependency `tomli_w`** (issue 023). Its only bundled copy
  today is vendored inside pip, so it disappears with this issue. Add it to
  the lock as a real runtime dependency with `tools/update_deps.py`, and add
  it to the build probe's import list.
- **The build probe also imports the packages of both UI programs,
  `gupkg-tui` and `gupkg-manager`, and of the migration tool** (issues 030
  and 023, decisions D34 and D45), so they are checked like the CLI.
- **Hooks.** Package hooks have no pip and no dependency installs. The
  rehaul's built-in services (issue 026) are designed to need only the
  standard library plus the bundle.
- **The registry tool and its CI** (issue 031, decision D36) run package
  checks through the release bundle, so they inherit a pip-free runtime.
  Their own tooling uses a separate `.venv`.
