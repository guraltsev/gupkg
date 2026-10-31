# T06: gupkg drops the copied registry data

Issue 036 F9, §4 decisions 3 and 4. Starts only after the owner has pushed the registry merge of T05 (controller,
"The registry repository (T05)").

## Goal

No gupkg test reads a copy of registry data. gupkg's providers, the package checker, the validation rules, the
fingerprints and the registry tool are tested on fixtures written for the test.

## Change

1. **Provider tests on crafted trackers.**
   - From the behaviour map of the four modules (`test_track_providers`, `test_registry_expressible`,
     `test_registry_trackers`, `test_registry_semantics`), list every **provider** behaviour the moved per-package cases
     exercised: each provider kind, `version_from`, `tag_pattern`, chains and their budgets, checksum discovery, the git
     provider, the User-Agent rules, and so on.
   - Write one owner test per behaviour, on a small tracker and fake responses written for the test:
     - provider behaviour goes in `tests/services/test_track.py` (code: `src/gupkg/services/track/`);
     - tracker validation goes in `tests/model/` (code: `src/gupkg/model/trackspec.py`).
   - Each test is proven (brief §4).
2. **Delete:**
   - those four modules (what is left after step 1);
   - `tests/test_registry_100.py`;
   - the 50 `registry_patterns.json` cases of `test_regex_guard` and of `test_regex_runner`. The guard's own rule tests and
     the worker's translation tests stay;
   - `tests/fixtures/registry_100/`, `tests/fixtures/registry_patterns.json`, `tests/fixtures/http/` and
     `tools/record_http_fixtures.py` with its test.
3. **Move the other users of `registry_100` to small fixtures:** `test_registry_validate` (the session test of T03),
   `test_registry_tool`, `test_fingerprint`, `test_engine_distribution`, and `tests/support/http_fixtures.py`.
   `GUPKG_REGISTRY_TRACKERS` disappears.
4. **The release tier runs the registry's tests against this checkout:**
   - add a `release` test, or a step in `scripts\test.cmd --all`, that runs `gupkg-registry\_scripts\test.cmd` with
     `GUPKG_REPO` set to this checkout;
   - skip it with a clear reason when `gupkg-registry` is not present;
   - choose one of the two; say which and why.
5. **Docs:** update `_docs/development_guide.md` and any docstring or README that names the deleted fixtures.

## Done when

- [ ] `git grep -n "registry_100\|registry_patterns\|GUPKG_REGISTRY_TRACKERS\|fixtures/http" -- tests src tools _docs`
      prints nothing.
- [ ] Every provider behaviour of step 1 has an owner test, listed in your log with its proof.
- [ ] `scripts\test.cmd --all` passes, and the release tier ran the registry's tests (paste their counts).
- [ ] `tools\test_report.py` before and after: the size of `tests/fixtures/` and the size of the test code.
