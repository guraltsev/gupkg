# Issue 028 worker log

Branch: rehaul/issue028 (from main 0d69dd0). Baseline on main: 2122 passed, 0 failed, 0 skipped (235 s).

## Plan: files

New core, `src/gupkg/engine/`: `context.py` (`Engine`), `planner.py` (pure planning), `executor.py` (phases, rollback
rules, receipt and install record), `appbuild.py` (atomic population), `effects.py` (desired effects, apply, remove,
verify), `portable.py` (persistence, markers), `fingerprint.py` (App and version fingerprints), `receipts.py`,
`stateio.py` (`[[adhoc]]`, `track.toml`), `checkupdates.py`, `health.py` (`check`), `errors.py`, `lifecycle.py` (entry
points, batch, cancel, elevation once). New pure data: `src/gupkg/model/plan.py`.
Changed: `model/settings.py` (`[install] record_manifest`), `system/port.py` and `system/fake.py`
(`ProcessNotAllowed`), `catalog/catalog.py` (`registry_entry`), `services/gitenv.py` (trusted git), design text.
Migration tool part B in `src/gupkg_migrate/`. Tests: `tests/engine_support.py` and `tests/test_engine_*.py`.

## Readings of unclear text (decisions)

See the report; the list is kept at the end of this file when the work is done.
