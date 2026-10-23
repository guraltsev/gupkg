# Issue 026: Built-in services replacing hook scripts

Date: 2026-10-22
Priority: High
Epic: 020. Design: `_docs/rehaul_design.md` §8

## Problem

The registry has 62 hook files (42 `check_update.py`, 17 `unpack_app.py`, 1
`populate_app.py`, 2 `install_step`), about 5,200 lines in total. Most of
them are copies of a few patterns, and the copies are inconsistent with each
other.

### Version discovery

| Pattern | Copies | Packages |
| --- | --- | --- |
| Static page scraped with a regex | 9 near-identical, plus 7 bespoke variants | graphviz, hwinfo, hwmonitor, irfanview, lockhunter, openhardwaremonitor, rapidee, testdisk, winscp; bespoke: 7zip, SumatraPDF, cwrsync, tixati, dontsleep, xnviewmp, shutter-encoder |
| Hand-rolled GitHub API client | 6 | bcu, perl, portablegit, wumgr, systeminformer, msys2 |
| Directory-index walk | 5 | emacs, gimp, python, vlc, pidgin |
| Redirect resolver | 5 byte-identical | crystaldisk×2, geogebra×2, zotero |
| Vendor JSON or text API | 6 | firefox, julia, vscode, ffmpeg, qbittorrent, restic-backup-gx |
| `Last-Modified` header as the version | 2 | autoruns, processexplorer |

### Unpacking and preparation

- "7z x, then flatten one wrapper folder": 6 copies, plus 15
  `extract = [{src="x-*/", dest=""}]` rows that do the same.
- Taking a subfolder or nested archive out of an installer: firefox,
  obsidian, mogan, pidgin.
- Deleting files after unpacking: keypirinha, mogan, pidgin, systeminformer.
- Renaming an executable through a hook: lockhunter, restic-backup-gx.
- Copying a shim to several names: volta.
- Post-unpack existence checks: 5 packages.

### Checksums and comparison

- Checksums are discovered in 13 different ways.
- 5 different version comparators exist.

### Bugs these copies have caused

- Several hooks ignore `appReady`, so a missing `App` is never repaired
  (vscode, 7zip, SumatraPDF, vlc, qbittorrent).
- msys2 never returns "current".
- hwinfo and irfanview format versions wrongly once the major version has two
  digits.
- testdisk can select a WIP pre-release.
- qbittorrent's `populate_app.py` downloads by itself, without a checksum.
- Every hook has its own User-Agent, timeout and retry policy.

## Scope

1. **`services/http.py`.** One HTTP client, over `System.fetch` (issue 021):
   - a User-Agent policy, configurable per package with `headers`;
   - timeouts, and retries with backoff on 5xx and 429;
   - TLS through `tls.py`;
   - the `GITHUB_TOKEN` header when it is set;
   - `Content-Disposition` handling;
   - resumable downloads into a cache keyed by URL and SHA-256.
2. **`services/track/`.** The providers, with the fields given in §8.1:
   - `github` (with `asset_pattern`, `version_from`, `tag_pattern`,
     `release = newest-matching`, `prerelease`)
   - `gitlab`
   - `page`
   - `index`
   - `redirect`
   - `json` (a JSONPath subset: dotted keys, `[n]`, `[?key==value]`)
   - `last_modified`
   - `rss`
   - `git`
   - `chain`
   - `module`

   Every provider returns `TrackResult(version, captures, checksum)`, and
   **the version comparison lives in the catalog, not in providers**.
3. **`services/unpack/`.** The `kind` values `zip`, `7z`, `tar`, `sfx`,
   `nsis`, `msi`, `exe`/`file` and `git`, detected from the file's
   signature and extension. Every kind supports `strip`, `subdir`, `nested`,
   `extract`, `require` and `verify_manifest`.
   - **`tar`** (D39) covers `.tar.xz`, `.tar.gz` and the other compressed
     tarballs, with two 7-Zip passes (decompress, then unpack the tar).
     MSYS2 uses its `.tar.xz`.
   - **There is no `inno` kind in 1.0** (D39). GIMP keeps running its
     vendor installer.
   - **`verify_manifest = "<path>"`** (design §8.2, §20). The artifact ships
     a `<sha256>  <relative path>` manifest. Every unpacked file must match
     it, and there must be no extra or missing files. gupkg's own release
     ZIP (`gupkg/MANIFEST.sha256`, issue 016) is the first user, and the
     service is general.
   - **Safe extraction:** no escaping paths, no reparse points, and staging
     with cleanup in `finally`.
   - **7-Zip:** the bundled `7zip\7z.exe` is the default, run through
     `System.run`.
4. **`services/prepare.py`.** The `remove`, `rename` (glob with exactly one
   match, never overwriting), `copy`, `write` and `module` steps. All of them
   are confined to the staged `App`. The built-in download script runs a
   package's `[[prepare]]` steps after unpacking, and a custom populate
   script can run the same steps through its context (D27).
5. **`services/checksum.py`.** Checksum discovery through `sidecar`,
   `sums_file`/`entry`, `json` and `page_match`, with GitHub and GitLab
   digests picked up implicitly. Verification itself is in issue 027.
6. **`services/hooks.py` and `gupkg.hooklib`.**
   - Hook API 2: a typed `HookContext` carrying the reporter, the HTTP client
     and the paths.
   - Entry points: `track(ctx) -> TrackResult`, `unpack(ctx)` and
     `prepare(ctx)`.
   - The built-in download script downloads `download_url` and verifies it
     (issue 027). A custom populate script (D27) may fetch what it needs
     through the context's HTTP client.
   - **Network (§9.3, D28).** Every hook gets gupkg's shared HTTP client in
     its context, including unpack, prepare and populate hooks in a version
     directory; Python cannot sandbox trusted code, so version-directory
     code may use the network. What stays forbidden, and is enforced by
     validation, is update discovery in a version directory: no `[track]`
     table and no `track` hook. There is no import warning.
   - `hooklib` re-exports the helpers listed in §8.6.
   - API 1 hooks are rejected like any other invalid hook. Core carries no
     API 1 or format 1 hint (D33).
7. **Service hints.** `gupkg pkg check` reports a hook that matches a known
   service pattern, together with a suggested format 2 replacement. The
   separate migration tool (issue 023) offers the same suggestions when it
   converts a package.

## Acceptance criteria

- [ ] Each provider has tests against recorded responses taken from the real
      registry packages named in §8.1, stored as HTTP fixtures under
      `tests/fixtures/http/`. They are recorded once by a script under `tools/`
      and replayed offline.
- [ ] Each unpack kind has tests against archives built at test time,
      including a nested archive, a wrapper folder, a `.tar.xz` and a
      `.tar.gz`, an escaping path (which must be rejected) and a `require`
      failure. The NSIS and SFX tests use a small committed sample with a
      documented origin. A manifest that asks for `kind = "inno"` fails
      validation.
- [ ] The registry's firefox, obsidian, pidgin, keypirinha, lockhunter,
      volta, hwinfo, crystaldiskinfo, autoruns, msys2 and julia packages can
      be expressed with no hook. This is shown by format 2 manifests
      committed as test fixtures that pass the format 2 validator (`gupkg pkg
      check` once issue 029 lands).
- [ ] A version-directory hook receives a `HookContext` with the shared HTTP
      client, and a request it makes through it carries gupkg's User-Agent
      and timeout policy. The validator rejects a version directory that has
      a `[track]` table or a `track` hook, and does not warn about a hook
      that imports `urllib`, `http`, `socket`, `requests` or `subprocess`.
- [ ] An unpacked tree with an extra, missing or modified file relative to
      its `verify_manifest` fails the unpack and leaves nothing staged.

## Out of scope

Converting the registry. That is issue 031, which uses these services.

## Closing notes

- **Accepted as delivered (owner decision):** the provider fixtures under `tests/fixtures/http/` are
  synthetic, modelled on the registry hooks' regular expressions and marked `"recorded": null`, and the
  NSIS and MSI kinds are tested with a 7-Zip process double. Each fixture carries its own `expected` block;
  `tools/record_http_fixtures.py` records the whole tracker walk against the live sites and rewrites it.
- **Owed to issue 031 and the owner:** (1) record the fixtures with `tools/record_http_fixtures.py` and
  review the diff (an explicit acceptance item of issue 031, plus manual check 1 of `tests/manual_smoke.md`);
  (2) commit a small real NSIS sample when the owner provides one, and replace the NSIS process double.
- **Delivered here, not in 028 (owner decision):** the subprocess regex runner. Every manifest pattern applied to
  fetched text runs in `gupkg/system/regex_worker.py` through `System.regex_worker` with a hard deadline
  (kill of the process tree, fail closed; design section 8.1). The static guard is only a first filter.
  The runner does not cap memory. Issue 028 only wires an elevated run: the trust checks of
  `gupkg.services.hooks.require_trusted_code` and `gupkg.services.http.github_environment`, and resolving
  the version-control and ssh programs from machine or admin-only paths (they are found through the
  inherited `PATH` today). Also owed to 028: in an elevated run the worker's interpreter
  (`sys.executable`) and `gupkg/system/regex_worker.py` (found today as `Path(__file__)` beside
  `windows.py`) must come from an admin-only location, with that trust checked before the worker is
  launched, the same rule as for package code.
- **Runner cost:** one worker process per call (50 to 400 ms to start here); a release run is two calls
  (all tags, all asset names) when the asset pattern does not depend on the release, and a runner stops
  with a clear error after 120 s of wall-clock time (`WALL_SECONDS`), separate from its 10 s of matching.
