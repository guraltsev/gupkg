# Development architecture

> This page describes the 0.17 architecture as it is implemented. The 1.0
> rehaul replaces it. New work should follow
> [rehaul_design.md](rehaul_design.md) and the issue that owns the area
> (epic: `_issues/issue020-rehaul-epic.md`).

`src/gupkg/cli.py` is the sole command-line boundary: it owns grammar, one-time
context resolution, dispatch, rendering, and exit translation. `gupkg.py` owns
public single-package workflows and has no CLI compatibility entrypoint.
Significant implementation domains live directly in `src/gupkg/`.

The runtime package has one-way domain boundaries:

- `core` provides dependency-light models and utilities.
- `windows` isolates operating-system integration.
- `layout` resolves package identities and activates `current`.
- `configuration` normalizes and validates the canonical TOML schema.
- `metadata` synchronizes directory-owned TOML fields without rewriting
  unrelated content.
- `components` applies shortcuts, environment variables, `PATH`, and wrappers.
- `origin` prepares and replaces `App/` payloads.
- `updates` owns state, hook loading, candidate normalization, and staging.
- `registry` owns the ZIP download/cache lifecycle and validates registry
  data without importing package-local code.
- `commands` is the application layer: every operation the front ends perform
  (package requests, `config-fix` via `configfix`, bulk manager updates, registry
  install, manager init, self repair) returns an `outcome.Outcome`.
- `cli`, `tui`, and `manager_tui` are thin shells that only collect input, call
  `commands`, and display the result with the shared formatters in `outcome`.
- `distribution` owns standalone runtime health, bootstrap metadata checks, and
  scoped self-repair.

The CLI imports only the names needed by its workflows. It does not provide
compatibility aliases or a provider framework. Update payload preparation
supports a small declarative sequence of built-in payload and trusted
package-local module steps; ordinary component installation remains fixed. New
implementation code should be placed in the module that owns its state or side
effect. Legacy format conversion remains implemented in
`legacy_to_gupkg_toml.py`; `gupkg config-fix` coordinates its explicit public
conversion result.

## Manager integration

Manager mode is an orchestration layer over collection discovery and the
existing single-package operations. `manager.py` owns configuration validation,
scoped inventory, planning, and the batch executor; the CLI and manager TUI
must call those same plan/executor boundaries. Planning may check providers and
update package-owned check state, but it must not download or activate a
version. The executor revalidates ownership and health before each target and
records skipped, failed, upgraded, and not-attempted outcomes.

The TUI deliberately keeps planning, confirmation, and execution on separate
scrollable screens. Worker tasks must leave Textual's event loop free while
provider and package operations run. Cancellation is a boundary request: it
prevents another target from being scheduled and never interrupts an operation
already in progress. Mixed-scope elevation is resolved before invoking the
executor. Per-target activation passes the manager installation context, so
wrappers land in the configured `[bin]` directories rather than the scope
defaults. Update checks capture process-wide stdout and therefore run one at a
time. Long work runs in thread workers and reports through callbacks
(`on_progress`, `cancel_requested`) so the screen can show `Checking i of N`
and one result line per target; the same callback shape serves refresh and
planning. Replaceable progress uses `core.log_progress`: it overwrites one line
on a console or a `LineStream` with `on_transient`, and writes only the final
line when output is redirected, so logs and `--format toml` stay small.
Manager discovery restores each target's last update status from the
package-owned `.gupkg/state/update.toml`; it reads that file without modifying
it (the update workflow owns renaming a corrupt file) and shows `current` when
a saved candidate is no newer than the active version. Opening a package from
the browser exits the manager app, so the manager records the browser filters
and highlight and reopens the browser there on return. Manual Windows coverage for UAC, missing roots, duplicate selectors,
partial failures, and terminal dimensions lives in `tests/manual_smoke.md`.

Manager schema v2 adds separate package roots, wrapper/bin roots, and a
registry cache. Configuration discovery and migration are implemented in the
manager layer; the operational contract is documented in
`docs/operations.md`. Registry selectors are resolved only after the active
cache has passed manifest and path validation.

## Update coordinator

The public update coordinator in `src/gupkg/gupkg.py` resolves the active package,
validates its `[update]` table, acquires the package-root lock, checks for a
candidate, asks the `updates` module to stage a complete version under
`.gupkg/work`, and atomically commits it. Check, download, and full update share
that one locked session. `gupkg update --download-only` leaves the staged
version inactive with a pending receipt; a full `gupkg update` reuses a version
whose receipt is still pending and activates it. Workflows return errors in
their `ActionResult` rather than printing them; the CLI and TUIs render them. Check and
unpack hooks are imported from `pkg.local/` as trusted
in-process Python extensions; they are never executed as shell commands.

Git payloads, including bootstrap `_bootstrap` packages, create a new
timestamped version directory. The update model has no mutable in-place mode
and no automatic update policy.

Installing a normal Git-backed `_bootstrap` template enters the update coordinator
before origin population or junction management. The resolved commit is staged
directly into the timestamped version, so `_bootstrap` never becomes an installed
payload.

The same coordinator accepts a `_bootstrap` template backed by a trusted
module check and ZIP or module payload. Release discovery remains
package-specific while bootstrap staging stays generic.

A Git origin defaults to `refs/heads/main` and supplies the default Git update
check, so package metadata needs only one source URL and ref. Explicit update
checks remain available for checkout-path or remote-name customization.

Update work, locks, timing state, and receipts are manager-owned data beneath
the package root's `.gupkg/` directory. A finalized version directory contains
only package-authored files and its completed `App/` payload.

### One distribution, built in the development tree

gupkg is distributed as one self-contained release ZIP,
`gupkg-<version>-win-amd64.zip`. Everything that acquires or verifies its
contents at build time lives in `tools/` and `scripts/` of the development tree
and is never shipped or reachable from `gupkg`; installed code only *checks*
integrity data:

| Tool | Role |
| --- | --- |
| `tools/fetch_runtime.py` (`scripts\fetch-runtime.cmd`) | The only place that pins the embeddable CPython URL and SHA-256. Fetches and verifies it, writes `._pth`, installs `tools/gupkg-deps.lock` with `--require-hashes --only-binary=:all: --no-deps`. |
| `tools/update_deps.py` | Resolves newer releases, shows the plan, rewrites the lock and refreshes the committed runtime's `site-packages` (`--repair` reinstalls the lock). |
| `tools/check_deps.py` | Compares the lock with PyPI (exit 1 when pins are stale). |
| `tools/build_standalone.py` (`scripts\build-standalone.cmd`) | Verifies the inputs, assembles the version tree, writes `gupkg/RELEASE.txt` and `gupkg/MANIFEST.sha256`, zips it and prints the ZIP's SHA-256. |
| `tools/release.py` (`scripts\release.cmd`) | Builds, tags, uploads the ZIP to GitHub release `v<version>` and records the digest in `RELEASES.md`. |

At run time the launcher starts only `python\python.exe` from its own folder
(`-I -s -B -S`); there is no system-Python lookup, no run-time pip and no
dependency probe. `gupkg.integrity` is the checking half of the manifest:
`manager self status`, the self-update hook (`pkg.local/unpack_app.py`) and the
tests use it. `src/gupkg/python/` stays committed so a checkout runs without a
build step. The ZIP digest in `RELEASES.md` covers the manifest in transit; after
installation it relies on admin-only folder permissions.

## Development environment

The dev tree uses one explicit interpreter and never probes for another.
Create `.venv` once, with any Python 3.11+ you choose:

```
<python> -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
```

Every script in `scripts\` uses `.venv\Scripts\python.exe` only; without it they
exit 9009 and print this command. `requirements-dev.txt` holds pytest and the
same runtime package versions as `tools/gupkg-deps.lock`. Run the tests with
`scripts\test.cmd`. To try gupkg by hand, run `scripts\run.cmd <arguments>`: it
starts `src\gupkg\gupkg.cmd`, so gupkg runs on its bundled Python like it does
for a user. `python -m gupkg` works only on the bundled Python, so tests that
start gupkg as a process use `src\gupkg\gupkg.cmd`.
