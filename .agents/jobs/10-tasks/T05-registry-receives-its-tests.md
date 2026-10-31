# T05: The registry receives its data tests (repository gupkg-registry)

Issue 036 F9, §4 decisions 3 and 4. Follow the controller's "The registry repository (T05)".

## Goal

Every check of the registry's **data** runs in `gupkg-registry\_tests` against the live registry, with gupkg's code taken
from the gupkg checkout (`GUPKG_REPO`). After this task, gupkg (T06) can delete its copies.

## Move or add, in `gupkg-registry`

1. **Per-package tracker cases.** The cases of `test_track_providers`, `test_registry_expressible`,
   `test_registry_trackers` and `test_registry_semantics` that test a *specific package's* tracker against its vendor
   responses. They go into `_tests/test_trackers.py`, which runs each live tracker against its recorded responses.
2. **The vendor responses:** gupkg's `tests/fixtures/http/*.json` move to `_tests/fixtures/http/`. Add cases for the
   packages that have a tracker but no case (the live registry has 95 packages; the snapshot had 79). If recording one
   would need the network, list those packages instead: the owner records them.
3. **`tools/record_http_fixtures.py`** moves to `_tools/record_http_fixtures.py`, with its test.
4. **The data checks of `test_registry_100` that the registry lacks:**
   - the portable rules (`modes = ["portable"]` with a `[portable]` table; no other package has one; portable-only
     packages say so);
   - the package-specific checks (qbittorrent, zotero, msys2, GIMP, nano, sqlitebrowser), rewritten as checks of the
     current files;
   - "every pattern in every tracker passes the pattern guard".

   Do **not** move the four checks the registry already has (issue 036 F9).
5. **CI:** `.github/workflows/validate.yml` runs the moved tests from `_tests`. Remove the step that runs gupkg's modules
   ("Tracker fixtures"). Update the registry's CI test (`_tests/test_ci_workflow.py`) to match.

## In gupkg

Change nothing except your log `.agents/jobs/10-logs/T05.md`. The map of behaviours (brief §3) covers every moved or added
case, with its source in gupkg.

## Done when

- [ ] `gupkg-registry\_scripts\test.cmd` passes on `i036/T05` against gupkg `main`. Paste the counts.
- [ ] The workflow file no longer names any `gupkg/tests/` module (`Select-String validate.yml -Pattern 'gupkg/tests'`
      prints nothing).
- [ ] For three moved tracker cases: change the package's tracker pattern so it no longer matches, show the test failing,
      restore.
- [ ] Your log lists the packages that still have no recorded responses, if any.
