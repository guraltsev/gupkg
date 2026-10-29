# Development guide

This page summarizes the module map and the testing approach of gupkg 1.0. The full design, with the
decision table D1-D46 and the reasons, is [rehaul_design.md](rehaul_design.md) (sections 12 and 16 are
summarized here); the per-module description is [src/gupkg/README.md](../src/gupkg/README.md). Style and test
rules are in [python_rules.md](python_rules.md), [docstring_schema.md](docstring_schema.md) and [tests.md](tests.md).

## Module map

```text
src/gupkg/            core (the `gupkg` command)
  model/       pure data, no I/O: versions, pkg.toml format 2, settings, install modes, package state, plans, checksum policy
  catalog/     discovery and joins over the roots and registry caches; the only place package state is derived
  services/    built-in declarative operations: HTTP client, track providers, unpack, [[prepare]], checksum, fingerprints, hooks
  system/      THE side-effect boundary: port.py (the System protocol), windows.py (real), fake.py (tests and --sandbox)
  engine/      planner (pure), executor, appbuild (atomic App), effects, portable data, locks, check-updates, health, lifecycle
  report/      events, the Rich console renderer, log files, the --json documents
  selfmgmt/    gupkg's own tree: where it lives, MANIFEST.sha256 status, elevated-run trust checks
  launchers/   the one template of install.cmd, gupkg-tui.cmd and check-updates.cmd
  modes.py, relaunch.py, distcopy.py   target -> mode, the one elevation step, copying a distribution into a root
  cli.py       the grammar -> lifecycle calls -> exit code; --json, --plan, --events-file, --no-input; the forwarders
src/gupkg_ui/         shared by the two UI programs; reads through core, never the engine
src/gupkg_tui/        gupkg-tui: one package's view
src/gupkg_manager/    gupkg-manager: the browser and the batch commands (check-updates --all, update --all, check)
src/gupkg_registry/   the registry maintainer tool (separate, not in the bundle)
```

Dependency rules:

- Inside `gupkg`: `model` <- `catalog`, `services` <- `engine` <- `cli`. `system` is reached only through the `System` protocol that
  `engine` and `services` receive. Nothing below `cli` prints or reads input, and there are no function-local imports hiding cycles.
- The separate programs build on core (D44): `gupkg_ui`, `gupkg_tui`, and `gupkg_manager` may import core, and core
  never imports them. The UI programs use core only to read; every operation they perform is a `gupkg` call through the machine
  interface (`--json`, `--plan`, `--events-file`, `--no-input`), to the `gupkg.cmd` of their own tree.
- Core contains no code for 0.17 data (D33): no format 1 reader, no `_bootstrap` handling, no aliases.
- The Windows registry is written, never read to learn anything (D18); a test checks the behaviour through `FakeSystem`'s access log.

## Testing

- **One boundary.** `System` is the only way to create links, read or write environment and `PATH` values, create shortcuts, broadcast
  changes, ask about elevation, run processes or fetch URLs. `WindowsSystem` is real; `FakeSystem` keeps registry values, shortcut
  records and elevation state in memory, uses real temporary folders for files and serves URLs from a fixture map.
- **Plans are data.** Planner tests assert on `InstallPlan`; executor tests run a plan against `FakeSystem` and assert on files,
  environment, shortcut records and receipts. Nothing patches module attributes.
- **Sandbox mode.** `gupkg --sandbox DIR ...` runs the real command line with `FakeSystem` persisted under `DIR`; manual checks and `pkg check
  --install` use it.
- **Fixtures** (archives, HTTP pages) are built or recorded; no test uses the network. Tests that need real Windows are marked
  `windows_integration`, and the `WindowsSystem` contract tests also run against `FakeSystem` so the two cannot drift. A test
  double must write only under `tmp_path`, never relative to the current directory.
- Anything that needs UAC, the Start Menu, a real `PATH` broadcast or a real release is a numbered entry in
  [tests/manual_smoke.md](../tests/manual_smoke.md).
- The documentation is tested: `tests/test_documentation.py` checks that no link in `README.md` and `_docs/` points to a missing file or
  heading and that the README command reference lists the commands of `gupkg --help`.

## Package launchers and shims

A version directory carries `install.cmd` and `gupkg-tui.cmd` (a tracker folder carries `check-updates.cmd`), all generated from the one
template in `gupkg.launchers` by whichever gupkg runs `check-updates`, `pkg new/bundle/export`, or the registry tool.
They find a gupkg relative to themselves (the root forwarder, then the bundled copy) and forward their arguments through `cmd.exe`, so
a `%`, `^` or `"` inside a forwarded argument is re-expanded by `cmd`: call `gupkg` directly for such values (documented, not fixed).

The Python package folder of a release (`gupkg\gupkg\`) also holds `gupkg.exe`, `gupkg-tui.exe` and so on beside their `.config.toml` (a `cmd /d /s /c <name>.cmd` shim made by
`tools/build_standalone.py`). Their relative argument `gupkg.cmd` is resolved by `cmd.exe` against the *current directory*, not the shim's
folder, so they work only when started from their own folder. The shims that gupkg installs from `pkg.toml` do not use them: they target
`$App\gupkg.cmd` by absolute path. The tree's own `.cmd` wrappers are what the root forwarder, the package launchers and the UI programs call.

### One distribution, built in the development tree

gupkg is distributed as one self-contained release ZIP,
`gupkg-<version>-win-amd64.zip`. Everything that acquires or verifies its
contents at build time lives in `tools/` and `scripts/` of the development tree
and is never shipped or reachable from `gupkg`; installed code only *checks*
integrity data:

| Tool | Role |
| --- | --- |
| `tools/fetch_runtime.py` (`scripts\fetch-runtime.cmd`) | The only place that pins the embeddable CPython URL and SHA-256. Fetches and verifies it, writes `._pth`, installs `tools/gupkg-deps.lock` with the `.venv`'s pip (`--target`, cross-targeted at the bundled CPython, `--require-hashes --only-binary=:all: --no-deps`). |
| `tools/update_deps.py` | Resolves newer releases, shows the plan, rewrites the lock and refreshes the committed runtime's `site-packages` the same way the build installs them (`--repair` reinstalls the lock; `--python` names the interpreter whose pip does it). |
| `tools/check_deps.py` | Compares the lock with PyPI (exit 1 when pins are stale). |
| `tools/build_standalone.py` (`scripts\build-standalone.cmd`) | Verifies the inputs, assembles the version tree, fails if pip, setuptools or ensurepip is in it, writes `gupkg/RELEASE.txt` and `gupkg/MANIFEST.sha256`, runs the build probe on the staged tree, zips it and prints the ZIP's SHA-256. |
| `tools/release.py` (`scripts\release.cmd`) | Builds, tags, uploads the ZIP and its `.sha256` file to GitHub release `v<version>`, checks the uploaded bytes, and records the digest in `RELEASES.md`. |
| `tools/check_release.py` (`scripts\check-release.cmd`) | Downloads the published release and checks it against its `RELEASES.md` row (tags, SHA-256s, `RELEASE.txt`, `MANIFEST.sha256`). |
| `tools/bump_version.py` (`scripts\bump.cmd`) | Raises or sets the version in `_version.py`. |

**Where pip lives.** The bundled runtime (`src/gupkg/python` and every release)
has no pip, setuptools or ensurepip, and `tools/gupkg-deps.lock` does not list
them. pip exists only in the development `.venv` (`python -m venv .venv`, which
includes pip). Packages reach the runtime at one point, the build, which uses the
`.venv`'s pip to install hash-pinned wheels into the bundle's `site-packages`.
To refresh the committed runtime's `site-packages` run
`.venv\Scripts\python tools\update_deps.py` (add `--repair` to reinstall the
lock unchanged). The build ends with a probe: the staged
`python\python.exe -I -s -B` imports every top-level package of the lock (by
import name, read from each `RECORD`), the entry modules of the programs
(`gupkg.cli`, `gupkg_ui`, `gupkg_tui.cli`,
`gupkg_manager.cli`; see `PROBE_MODULES` in `tools/build_standalone.py`), and
asserts pip and ensurepip are not importable. A failing probe fails the build.

At run time the launcher starts only `python\python.exe` from its own folder
(`-I -s -B -S`); there is no system-Python lookup, no run-time pip and no
dependency probe. `gupkg.integrity` is the checking half of the manifest:
`gupkg self status`, the populate script of gupkg's own package
(`pkg.local/populate.py`) and the tests use it. `src/gupkg/python/` stays committed so a checkout runs without a
build step. The ZIP digest in `RELEASES.md` covers the manifest in transit; after
installation it relies on admin-only folder permissions.

## Development environment

The dev tree uses one explicit interpreter and never probes for another.
`scripts\setup.cmd` creates `.venv` (it tries `uv venv --seed`, then `python -m venv`, so pip is always present)
and installs `requirements-dev.txt`; run it once, or let any other script run it when `.venv` is missing.
`scripts\setup.cmd C:\path\python.exe` picks the interpreter (3.11 or newer) and `--force` recreates the environment.
The release steps (`bump`, `release`, `check-release`) are in the Operations guide.

Every script in `scripts\` uses `.venv\Scripts\python.exe` only. `requirements-dev.txt` holds pytest and the
same runtime package versions as `tools/gupkg-deps.lock`. Run the tests with
`scripts\test.cmd`. To try gupkg by hand, run `scripts\run.cmd <arguments>`: it
starts `src\gupkg\gupkg.cmd`, so gupkg runs on its bundled Python like it does
for a user. `python -m gupkg` works only on the bundled Python, so tests that
start gupkg as a process use `src\gupkg\gupkg.cmd`.
