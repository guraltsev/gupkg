# Issue 032 log: migration path, documentation, issue hygiene

Branch `rehaul/issue032` from main d48404a. Baseline: 3760 passed (issue 033 log).

## Changed
- Docs: README (format 2 reference, layout, trackers, checksums), `_docs/` getting_started, cookbook, operations, troubleshooting,
  development_guide, README index, rehaul_design (implemented + deviations), `docs/elevated-runtime-design.md` moved to `_docs/` and
  brought current, RELEASES.md migration section, `tests/manual_smoke.md` header rewritten around `--sandbox` plus entries 032-1..4.
- Tests: `tests/test_documentation.py` (links, README vs `--help`).
- Pickups: bootstrap calls install.cmd directly; `\System32` escape; `_follow` kills once; `clamp_child_exit_code` int check;
  `nul .txt` device names; installer test double fails on a relative stage.
- Issues 020-033 moved to `_issues/_closed/` with status lines; issue 014 re-targeted.

## Acceptance
- Link checker, help/README parity: test_documentation.py. Format 1 / config-check: existing tests (test_cli_grammar,
  test_manifest_format2). 0.17 migration and self update 1.0.0 to 1.0.1: manual 032-1, 032-3 (verification rules:
  test_self_release_update.py).

## Left open
- package-launcher.cmd `%`/`^`/`"` re-expansion: documented only. Shim `.exe` configs with relative `gupkg.cmd`: documented, unused by pkg.toml.
- Relaunch events file never pruned; exit 5 vs refused on a missing runtime; closure-test gaps; the kill-once change has no dedicated test.
