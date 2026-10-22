# Issue 026: Built-in services replacing hook scripts

Date: 2026-10-22
Priority: High
Epic: 020. Design: `_docs/rehaul_design.md` §8

## Problem

The registry has 63 hook files (42 `check_update.py`, 17 `unpack_app.py`, 1
`populate_app.py`, 2 `install_step`), about 5,300 lines in total. Most of
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
3. **`services/unpack/`.** The `kind` values `zip`, `7z`, `sfx`, `nsis`,
   `msi`, `inno`, `exe`/`file` and `git`, detected from the file's
   signature and extension. Every kind supports `strip`, `subdir`, `nested`,
   `extract`, `require` and `verify_manifest`.
   - **`verify_manifest = "<path>"`** (design §8.2, §20). The artifact ships
     a `<sha256>  <relative path>` manifest. Every unpacked file must match
     it, and there must be no extra or missing files. gupkg's own release
     ZIP (`gupkg/MANIFEST.sha256`, issue 016) is the first user, and the
     service is general.
   - **Safe extraction:** no escaping paths, no reparse points, and staging
     with cleanup in `finally`.
   - **7-Zip:** the bundled `7zip\7z.exe` is the default, run through
     `System.run`.
   - **`inno`:** a bundled innoextract is preferred, if its licence allows
     bundling. Otherwise 7-Zip covers formats it can open, and
     `[source.installer]` is used as an explicit fallback.
4. **`services/prepare.py`.** The `remove`, `rename` (glob with exactly one
   match, never overwriting), `copy`, `write` and `module` steps. All of them
   are confined to the staged `App`.
5. **`services/checksum.py`.** Checksum discovery through `sidecar`,
   `sums_file`/`entry`, `json` and `page_match`, with GitHub and GitLab
   digests picked up implicitly. Verification itself is in issue 027.
6. **`services/hooks.py` and `gupkg.hooklib`.**
   - Hook API 2: a typed `HookContext` carrying the reporter, the HTTP client
     and the paths.
   - Entry points: `track(ctx) -> TrackResult`, `unpack(ctx)` and
     `prepare(ctx)`.
   - Hooks may not download artifacts; the engine downloads.
   - **Network boundary (§9.3).** Only `track` hooks in `_check_updates` get
     the HTTP client. Unpack and prepare hooks in a version directory get a
     `HookContext` without one. `gupkg pkg check` warns when a
     version-directory hook imports `urllib`, `http`, `socket`, `requests` or
     `subprocess`.
   - `hooklib` re-exports the helpers listed in §8.6.
   - API 1 hooks are rejected, with a pointer to `gupkg pkg migrate`.
7. **Service hints.** `gupkg pkg check` reports a hook that matches a known
   service pattern, together with a suggested format 2 replacement. The
   same code is used by the migration tool (issue 032).

## Acceptance criteria

- [ ] Each provider has tests against recorded responses taken from the real
      registry packages named in §8.1, stored as HTTP fixtures under
      `tests/fixtures/http/`. They are recorded once by a `_tools` script
      and replayed offline.
- [ ] Each unpack kind has tests against archives built at test time,
      including a nested archive, a wrapper folder, an escaping path (which
      must be rejected) and a `require` failure. The NSIS and SFX tests
      use a small committed sample with a documented origin.
- [ ] The registry's firefox, obsidian, pidgin, keypirinha, lockhunter,
      volta, hwinfo, crystaldiskinfo, autoruns, msys2 and julia packages can
      be expressed with no hook. This is shown by format 2 manifests
      committed as test fixtures that pass `gupkg pkg check`.
- [ ] No hook in the test fixtures performs its own HTTP download.

## Out of scope

Converting the registry. That is issue 031, which uses these services.
