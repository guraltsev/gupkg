# Issue 016 worker log

Branch: rehaul/issue016 (from main 133f401).
Baseline: 257 passed, 0 failed, 0 skipped. Final: 271 passed, 0 failed, 0 skipped.
The worktree has no .venv and the committed runtime has no pytest, so the suite is run with
the main checkout's `.venv\Scripts\python.exe -m pytest -q` (read-only use of that interpreter).

## Files changed

- Deleted: src/gupkg/dependencies.py, locked_deps.py, dependency_notices.py, digest.txt.
- Edited: gupkg.cmd, bootstrap.py, cli.py, commands.py, gupkg.py, updates.py, manager.py,
  manager_tui.py, tui.py, distribution.py, both pkg.toml, src/pkg.local/*.py,
  gupkg-bootstrap.ps1, RELEASES.md, .gitignore, scripts/build-standalone.cmd, release.cmd.
- New shipped: src/gupkg/integrity.py.
- New dev tree: tools/fetch_runtime.py, update_deps.py, check_deps.py, gupkg-deps.lock (moved from
  src/gupkg), scripts/fetch-runtime.cmd; rewritten tools/build_standalone.py, tools/release.py.
- Docs: README, _docs/operations.md, troubleshooting.md, development_guide.md,
  docs/elevated-runtime-design.md (superseded note), src/gupkg/README.md, tests/manual_smoke.md.

## Acceptance criteria -> coverage

See the final report; each criterion maps to a test in tests/test_launcher.py,
test_no_runtime_acquisition.py, test_update_hooks.py, test_release_bundle.py,
test_self_release_update.py, test_bootstrap_script.py, test_release_tool.py,
test_update_deps_tool.py, test_runtime_tools.py, or to tests/manual_smoke.md "issue 016" steps 1-5.

## Decisions taken (issue text unclear)

- ZIP layout in 0.18: one top folder `gupkg-<version>/` holding the version directory (pkg.toml,
  pkg.local, gupkg.cmd, gupkg-tui.cmd, gupkg\ with python\, MANIFEST.sha256, RELEASE.txt). The
  0.17 engine still needs the tree at the version root and the App\RELEASE.txt marker (written by
  unpack_app), as the issue says.
- MANIFEST.sha256 paths are relative to the version directory (so pkg.toml, pkg.local and the
  launchers are covered, not only gupkg\). Install state (App, Icons, Shortcuts, gupkg\gupkg-config.toml,
  __pycache__) is not "extra".
- `manager self update` previously only repaired. The acceptance list says it installs the next
  release, so it now runs the ordinary update workflow on the running version directory; `repair`
  is unchanged.
- The 1.x guard lives in check_update (raises with the bootstrap command), so every 0.x update path
  (gupkg update, manager update, self update, TUI) refuses a 1.x release and changes nothing.
- RELEASES.md gets a SHA-256 column; rows before it carry `-` and cannot be installed by the updater.
- Old `[self] dependency_update_check` in manager config is still accepted and ignored.
- "gupkg manager self repair with a ZIP" (issue intro) is not implemented: it is not in the scope
  table; the documented fix is gupkg-bootstrap.cmd -Source <zip>.
- Acceptance grep: src/gupkg/python (the committed runtime, which includes pip) is excluded; its
  removal is issue 019.

## Review round 1

- Build passes --no-compile to pip and deletes __pycache__/*.pyc before the manifest; integrity no longer exempts __pycache__ (planted .pyc is reported extra). The launcher's -B and the hooks' dont_write_bytecode keep an installed tree clean; a test runs the bundle and re-verifies it.
- manager doctor now also reports gupkg's own manifest differences (silent for a source checkout without a manifest).
- Bootstrap checks a reused version folder against its manifest (install state exempt), rejects duplicate manifest paths, and extracts with .NET after checking each entry stays under the destination.
