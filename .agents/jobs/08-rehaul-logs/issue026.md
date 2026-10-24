# Issue 026 worker log

Branch: rehaul/issue026 (from main 1b548b3). Baseline on main: 965 passed, 0 failed, 0 skipped (venv Python 3.14, run
from the worktree; the whole suite takes about 4 to 6 minutes).

## Files

- Port (changed): `system/port.py` (`HttpResponse`, `DownloadResponse`, `ResponseTooLarge`; `System.http_request`,
  `System.http_download`), `system/windows.py` (real transport: no redirect following, TLS through `tls.py`, only
  http/https/file), `system/fake.py` (`FakeHttp`, `RecordedRequest`, `request_log`, range and interruption simulation).
- Model (pure): `model/patterns.py` (bounded regex use), `model/jsonpath.py` (JSONPath subset), `model/trackspec.py`
  (value validation and typed provider specs); `model/manifest.py` (changed: key sets, `[source] headers`, value validation).
- Services: `services/http.py`, `values.py`, `safepath.py`, `checksum.py`, `hooks.py`, `prepare.py`, `populate.py`, `hints.py`,
  `track/` (`common`, `page` = page/rss/chain, `index` = index/redirect, `vendor` = json/last_modified, `releases` =
  github/gitlab, `repository` = git/module), `unpack/` (`extract`, `sevenzip`, `detect`, `transform`, `gitsource`).
- `gupkg/hooklib.py` (hook API 2 helpers). `pkgcheck.py` (changed: hook API, hints, capture cross-check).
  `gupkg_migrate/hooks.py` (delegates to core hints), `gupkg_migrate/package.py` (API 1 errors become notes, stale
  "confirm with issue 026" notes removed).
- Tools/tests: `tools/record_http_fixtures.py`, `tools/build_standalone.py` (probe list), `tests/http_fixtures.py`,
  `tests/archive_support.py`, `tests/fixtures/http/*.json` (25 cases), `tests/fixtures/registry2/*/_check_updates/pkg.toml`
  (25 trackers), 14 new test modules.
- Docs: design 8.1, 8.2, 8.3, 8.5 and 8.6 gained the details below; `src/gupkg/README.md`, `port.py` docstring,
  `tests/manual_smoke.md` (section 026).
- Existing tests changed because of the new rules: `test_pkgcheck` (hook fixtures declare API 2), `test_migrate_package`
  (a converted package keeps 0.17 hooks, which 1.0 reports as notes), `test_catalog_registry` (trackers that still use a
  0.17 hook are dropped by the registry sync; the test now says so and uses czkawka), `test_core_without_migration_tool`
  (its `match` now needs a `version` group).

## Decisions taken inside the issue's room

- The port gains `http_request` and `http_download` next to `fetch`/`fetch_text` (which stay for the 0.17 engine). They never
  follow redirects and return any status with headers, so redirect, retry, header and token policy lives in
  `services/http.py` and is testable with `FakeSystem`. A contract test runs the same scenarios on the real transport (loopback
  server) and on `FakeSystem`.
- `[source] headers` and per-provider `headers` exist so a package can set a User-Agent or Referer for the check and for the
  download (hwinfo needs both). `[track]` key sets grew: `version`/`download`/`headers` for page-like providers; `records`,
  `match`, `checksum`, `captures`, `select` for json; `level` rows take `key`, `next`, `probe`; chain steps take `header`.
- `[[source.nested]] replace = true` means "the archive's contents replace what is at `dest`" (obsidian: dest "" drops the
  rest of the installer); without it files are merged and an existing file is never overwritten. The inner archive is deleted
  once unpacked.
- Option order in the unpack service: nested, strip, subdir, extract, require, verify_manifest (every path addresses the tree
  as it is at that point).
- `msi` is unpacked by 7-Zip (design allowed `msiexec /a` or 7z); no installer code is ever run.
- Hook entry points: `track`, `unpack`, `prepare`, `populate`; `gupkg.hooklib` helpers take the context first.
- A hook that does not declare `PKG_MODULE_API = 2` is an error in `pkg check` (plain message, no mention of API 1, D33). The
  migration tool turns that error into a note, so converting a package whose 0.17 hook still exists keeps working.
- Hints (core) do not use 0.17 entry-point names (D33): a file that reads the network is a discovery candidate.
- Checksum discovery: a configured source that has no digest is an error (`ChecksumDiscoveryError`); 027 decides what a missing
  hash means. GitLab's releases API publishes no digest, so a link `sha256`/`digest` is used only if present.
- GitHub token hosts: `api.github.com`, `github.com`, `uploads.github.com`, https only; dropped on any cross-host redirect.

## Findings while building

- The first version of the client forgot to drop credentials on a cross-host redirect; the test written for the rule found it.
- Mutation run (20 mutants of the security rules: path checks, link checks, size limits, manifest check, token, credential
  stripping, downgrade, regex shape and run cuts, rename overwrite, confinement, API check, doctype, probe, prerelease): 18
  killed at once; one survived because it is equivalent (the `..` segment is also refused by the trailing-dot rule); one
  (prerelease) survived and got a new test.
- Python's `re` cannot be interrupted. Defences: nested unbounded repeats are rejected when the manifest is validated, long runs
  of word characters and of one repeated character are cut to 1024 before matching (both linear), at most 1 MiB is scanned,
  matches are capped. A page crafted against one specific pattern (long lines of mixed punctuation under a leading `.*`) can
  still cost seconds. A subprocess-based runner would remove that; not done (see report).
- 7-Zip 26.03 prints `Symbolic Link =`, `Hard Link =`, `Mode = l...` and, for zip, hex attributes whose high half is the Unix
  mode; the listing parser reads all of them.

## Not done / gaps

- Fixtures are synthetic (no network in the session): marked `"recorded": null`; `tools/record_http_fixtures.py` re-records them.
- No NSIS sample could be built offline: the NSIS and MSI kinds are covered with a 7-Zip process double (command lines and
  safety rules). The SFX test uses the bundled `7z.exe` as the stub of a real 7z archive built at test time.
- The custom-script runner of design section 12 (`populate/`) is only `run_custom_populate` here; the built-in download
  script (download, verify, activate) is issues 027 and 028.

## Review round 1 (after the merge of main with issue 025)

Merged main (025) with no conflicts. Fixes, by the reviewer's numbers:

1. Unpacking is bounded while it runs: gzip/bzip2/xz wrappers are decompressed in process with a byte cap
   (`decompress_capped`); every 7-Zip run gets a folder watcher through the new `System.run(watch=...)`
   (`ProcessAborted`), which measures the staging folder and kills 7-Zip past the size or entry limit; limits are one
   `Budget` shared by an artifact and its nested archives; default entry limit 200,000. Tests: `test_unpack_bombs.py`
   (bz2 and gzip bombs, gzip with a lying trailer: the partial output is never larger than the cap, a watcher kill,
   cumulative nested limits).
2. Regex guard (interim): overlapping alternatives, empty-matching body, a leading wildcard with other unbounded
   repeats, more than 3 wildcard repeats or 8 unbounded repeats are refused at validation; lines are cut to 8 KiB.
   The reviewer's pairs are tests run in a child process with a hard timeout: all refused or finished under 2 s.
   **Not bounded (for 028's runner):** any pattern that passes validation is not proven linear; text inside one
   8 KiB line can still cost O(line^2) for two or three wildcard repeats (round 2: the reviewer measured about
   40 s per MiB, so my earlier "0.5 s per MiB" claim was wrong and is withdrawn); a page designed against one
   accepted pattern can cost seconds; nothing can interrupt `re`. Superseded by round 2 below.
3. `pkgcheck` rejects API 1, no declaration, a string and a computed declaration (test); the migrate test now
   asserts the error exists for firefox before filtering it.
4. `load_hook`, `run_custom_populate` and the `[[prepare]] module` step take the `System` and, in an elevated run
   or with `GUPKG_ELEVATED=1`, refuse a version directory or `pkg.local` that is not admin-only before importing or
   starting anything (`require_trusted_code`, tests in `test_hook_trust.py`). For 027/028: call them with the real
   System; the engine must treat `HookError` from this check as a refusal to run package code, not as a package bug.
   `file:` URLs naming a network host or UNC share (`file:////server/share`, `file://host/x`) are refused by
   `HttpClient` unless `allow_unc_file_urls=True` (default refuse; decide in 027/028). The real transport still
   opens any `file:` URL it is given: the client is the gate.
5. Header names (`Host`, `Content-Length`, `Connection`, `Transfer-Encoding`, `Upgrade`, `TE`, `Proxy-*`,
   `Accept-Encoding`, `Range`...) are refused in manifests; names containing auth/key/token/secret/password/cookie/
   session are dropped on a cross-host redirect; every request and download has a total deadline enforced by the
   transport (`total_timeout`, `read1` so a trickling server cannot hide behind the idle timeout).
6. `HttpClient` reads no environment (`env` is an explicit mapping; `github_environment(system, environ)` drops the
   token in an elevated run); `git` runs through `services/gitenv.py`: allow-listed machine variables only, null
   config files, `core.hooksPath=NUL`, no credential helper, no prompts, no `GIT_SSH_COMMAND`/`GIT_DIR`/... (test).
7. Any exception in a hook's import-time code is a `HookError`; `run_prepare` reports it as a `PrepareError`.
8. `test_catalog_registry` pins the exact set of dropped trackers (the 48 packages of the 0.17 fixture that have a
   `pkg.local`) and their exact problem lines; the swap from firefox to czkawka stays because firefox is among the
   dropped (czkawka is the surviving tracker-only package).
9. Nits: `check_member_set(repeats=False)` for 7-Zip file entries (a tar repeating a name is refused); `[[source.extract]]`
   refuses two matches mapping to one destination; the boundary grep is a test (`test_service_details.py`) and the
   hint regex builds the opener name from pieces; a case-only rename is allowed; file names derive from the final URL
   skipping `/download` (`file_name_from_url`, used by downloads and the redirect provider); the fixture omissions:
   restored firefox's profile-manager shortcut and msys2's five shortcuts. The committed 0.17 snapshot of msys2 has
   no `config.default` step (if the live registry has one, `[[prepare]] write`/`copy` expresses it).

Owner decisions applied: provider fixtures stay synthetic; wording says "synthetic fixtures modelled on the registry
hooks' regexes, marked recorded: null, to be re-recorded by tools/record_http_fixtures.py"; a test asserts the marker;
expected values live in each fixture's `expected` block; `record_case` runs the real tracker and records the whole
walk (index walks, chains, redirect hops) and rewrites `expected`. **Owed to 031 / the owner:** recording the
fixtures (explicit 031 acceptance item and manual check 1) and a committed real NSIS sample.

## Review round 2

M1 regex guard: lines cut to 1 KiB, 4 KiB line-aligned windows, a 1 s budget between windows; text and design now say the guard is a mitigation, not a bound (worst of the 20 patterns x 7 hostile pages: 1.05 s; each pair refused or under 2 s in a child process with a 60 s timeout). M2 groups are looked through at any depth; an unbounded dot repeat only when sole. M3 the `file:` UNC gate decides on the decoded and converted path (12 spellings plus 4 local ones tested). M4 `System.run(watch=)` contract tests on FakeSystem and WindowsSystem (process tree killed). m1 absolute header deadline and `HTTPException` mapping (loopback hostile server). m2 truncated bodies and non-identity Content-Encoding are refused. m3 all 50 registry regexes validate. m4 docs reworded. m5 the watcher does not follow junctions (`windows_integration` test). m6 `tests/test_unpack_guards.py`; a 31-mutant run left no survivor. m7 trust also judges the script file and parent chain; `sys.exit` at hook import is a `HookError`. m9 xz memory limit 256 MiB with a crafted 4 GiB-dictionary test.

Owed to 028 (m8): git and ssh are resolved through the inherited PATH; elevated runs must resolve git from machine or admin-only paths. Also owed: the subprocess regex runner (the guard is not a bound).

## Review round 3 (one fix round)

1. Bounded repeats: nesting, empty-body and alternative-overlap checks now run for every repeat of four or more; iterations with inner repeats are accepted only with a required first or last character no inner repeat can consume (character sets, case folding included). All 13 reviewer cases are refused (subprocess tests); `(?P<v>\d+(?:[._]\d+)*)` is accepted; 50 of 50 registry patterns pass. A random fuzz of 4000 shapes accepted 1022, none slower than 0.3 s on five hostile short pages.
2. More than one unbounded dot repeat is refused.
3. Scan windows overlap by three lines (a match is found once); tests pin the 1 KiB cut and the overlap. Also found and fixed: `_cut_long_lines` copied the line at every cut (quadratic).
4. Concatenated xz streams are decompressed; padding must be a multiple of four zero bytes; junk is refused.
   (Round 4, below, supersedes the regex parts of items 1 to 3: the guard is a first filter, the runner is the bound.)
5. `WindowsSystem.run` kills the process tree on any exception from the wait loop (a raising watcher, Ctrl-C).
6. 304, 204, 1xx and HEAD answers skip the length check; headers cut by a closed connection are an error; Windows asks the watcher once more after the child exits, like the fake.
7. Documented: bodies without a length are accepted; sibling hook modules are covered only by the directory check; entry-point `sys.exit` is a HookError; `file://LOCALHOST` is case-insensitive.

## Review round 4 (final fix round): the regex runner is in 026

The static regex guard cannot bound `re` (the reviewer found `(?:\w+\s?){3}z`, `\d+\d+\d+x`, `(?:a+){3}b` and more that pass it and hang). The owner decided that the real fix goes into 026. Delivered:

- `gupkg/system/regex_worker.py`: a stdlib-only script (json, os, re, sys, time) run by `System.regex_worker` (a new port method; `WindowsSystem` and `FakeSystem`: the fake runs it for real unless a `regex-worker` double is registered, and logs requests in `regex_log`) with `-I -s -B -S`, an environment of only SystemRoot, an empty temporary folder as cwd, and the request as ASCII JSON on stdin.
- `gupkg.services.regexrun.RegexRunner`: a per-call deadline of 2 s counted from the worker's first line (`pid <n>`), 10 s total of matching per provider run (the worker reports how long it matched), a kill of the process tree on expiry (job object), `RegexTimeout` and `RegexError` (both `PatternError`), strict validation of the answer, fail closed. `RemoteMatch` offers group, groups, groupdict, start, end and span. Batches: `fullmatch_many`, `finditer_many`, `fullmatches` (the matcher that `model/jsonpath` now takes for `=~`; one call per condition per node set).
- Wired in: `candidates_from_text(s)` (page, rss, chain, index, redirect and json providers), checksum `page_match` and the JSON-path checksum, the GitHub and GitLab tag and asset patterns (a regex failure stops the provider, it never skips a release), JSON filters. `TrackEnvironment.regex` gives each provider run its own runner.
- `scan()` is now the plain in-process reference (no windows, no line cuts, no budget; the final empty match is returned, which fixes the 99 against 100 nit); the 1 KiB line cut, the windows and the overlap were removed because the deadline makes them unnecessary.
- `compile_pattern(..., flags=)` parses with the flags; the alternatives rule has its own tests.
- `WindowsSystem.run`: stdin is fed from a thread (a child that never reads it no longer blocks the timeout), every child is in a job object so a kill also reaches a helper whose parent already exited (the failing final watcher poll), and the job kills nothing on a clean finish.

Not covered: memory, OS-enforced no network, a long-lived worker (one process per call; a start takes 50 to 400 ms here, and the deadline starts after it).

Last touches after the runner review: the job object of a regex worker is made with kill-on-close (a hard-killed parent leaves no worker); job creation is inside the try (a failure kills the child); the worker deadline counts from the `pid` line (test with a late line); a start-limit overrun is `WorkerDidNotStart` with its own message; the no-leak test counts only processes of its own uniquely named copy of the worker; release runs match all tags and all asset names in two calls (`_Matches`); a runner stops after 120 s of wall-clock time; `_well_formed` rejects booleans and spans beyond the text.
