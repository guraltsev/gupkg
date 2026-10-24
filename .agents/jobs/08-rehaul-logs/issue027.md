# Issue 027 worker log

Branch: rehaul/issue027 (from main ad333bd). Baseline on main: 1839 passed, 0 failed, 0 skipped.

## Scope reading (027 vs 028 vs 029 vs 030)

027 delivers the policy and the pieces the engine, CLI and manager call. There is no engine (028), no CLI flag (029) and no
manager program (030) yet, so the acceptance rows that say "install exits 3" are covered here at the seam the engine
will call (`fetch_artifact`, `fetch_git`) against `FakeSystem` and a loopback server, and the CLI/manager wiring is a handoff note.

New modules:
- `gupkg/model/checksum.py` (pure): digest normalisation (one place), decision values, `ChecksumRecord` (receipt part),
  errors with exit code 3, the pre-download gate `check_before_download`, `parse_skip_flag`, `is_https_url`,
  `ChecksumPlan` (plan marking), the git gate and record.
- `gupkg/model/manager_policy.py` (pure): `[manager] checksum_missing` reader (user/portable layers ignored when elevated),
  the per-target action and the `--skip-checksum=<reason>` argument.
- `gupkg/services/artifact.py`: `fetch_artifact` (gate, download with https enforced on every hop, verification, record),
  `fetch_git`, `reverify`.
- `gupkg/pinning.py`: `pin_hash` (`gupkg pkg pin`): explicit, text-level edit, re-parsed and compared.
- `HttpClient.download(require_https=...)` checks the parsed scheme of the first URL and every redirect hop.
- `CommitMismatch` (an `UnpackError`) in `services/unpack/gitsource.py`.
- manifest: a targeted error for `ignore_checksum`.
- `tools/build_standalone.py`: probe list gains the new modules (they import under the bundled `-I -s -B -S` Python 3.12; checked).

Tests: `test_checksum_policy.py`, `test_checksum_fetch.py`, `test_manager_policy.py`, `test_pinning.py`.
Mutation check: 16 mutants of the key rules (https-only gate, mismatch comparison, final-URL check, per-hop check, digest
length, no normalisation, elevated user layer, non-skippable target asked, truthy answer, reverify, skip-decision check,
non-string digest, pin URL check, whitespace in URLs, user information) were each killed by the tests; one redundant first-URL
check in the client survived and was removed (the per-hop check covers it) and a direct client test was added.
One vacuous test was found and fixed on the way (the reverify test flipped a byte to the value it already had).

## Decisions made while reading the issue

- **The policy is a library plus seams, not a command.** Acceptance rows that say "install exits 3" or "check-updates reports
  the version as needing --skip-checksum" need the engine (028) and the flags (029). They are covered at the seam the engine
  calls: `fetch_artifact`/`fetch_git` raise `ChecksumMissing` (exit_code 3, code `checksum-missing`, `skippable`) before any
  request is made (tests assert an empty `request_log`, no connection and no cached file). The engine wraps those calls in the Verify phase.
- **Decision values and the flag.** Bare `--skip-checksum` = `skipped-cli`; the manager relays `--skip-checksum=setting`
  (`skipped-setting`) or `--skip-checksum=user-approved`. `parse_skip_flag` rejects any other value (fail closed). 029 should use
  `nargs="?", const="cli"` and call `parse_skip_flag`; the CLI passes the result as `skip=` and nothing else.
- **A declared hash wins over the flag**: the flag covers only missing hashes, so with a hash present the decision is
  `verified` even when `--skip-checksum` was given.
- **Fail-closed extras.** A malformed declared digest is `ChecksumInvalid` (not "missing"); an unknown skip decision is
  refused; `is_https_url` refuses user information (`https://good@evil`), backslashes, whitespace and control characters,
  `https:/x`, an empty host and a bad port. A redirect hop whose parsed form is not https stops a skipped download
  (`HttpClient.download(require_https=True)`), and the final URL is judged again in `verify_observed`.
- **Git sources.** A pinned `[source.git] commit` is the anchor (`verified`, the receipt records `commit`); a ref-only source follows the
  missing-checksum rule (https only, with the flag). The commit that was resolved is recorded.
- **Observed hash and TOCTOU.** The client already hashes the finished `.part` file before renaming it into the cache (resume
  makes hashing during streaming unsound). `reverify(fetched)` re-hashes the cache file; 028 must call it right before unpack or
  copy. Weaker than "hash while streaming"; flagged for the reviewer.
- **Receipt.** `ChecksumRecord.to_data()` is the `[checksum]` table 028 puts in the receipt (keys: decision, url, final_url,
  observed_sha256, size, sha256_from, commit, registry); `parse_checksum_record` is strict. `registry-fingerprint` and
  `origin-not-verified` are valid values for 028's no-download installs. `unverified_note(decision)` is the text for
  `list --details`, `info` and `check` (an unknown decision counts as unverified).
- **Plan.** `plan_checksum(url, sha256, url_from=..., commit=, git=)` gives `ChecksumPlan.to_data()` =
  `{sha256: present|missing, url, url_from, skippable}`. 029 puts it in `--plan --json`. A malformed hash is "missing".
- **Manager.** `manager_checksum_policy(settings, elevated=)` (user and portable layers ignored under elevation, an odd value means refuse),
  `decide_target(s)`, `skip_argument`, `command_line`. A non-https target is never asked about and never skipped. The CLI
  modules (`model/checksum.py`, `services/artifact.py`) import no settings and no `os`; a test enforces it.
- **pkg pin.** `pin_hash(version_dir, record)` refuses another URL, a git or script source, an existing different hash and a
  record with no observed hash; the text edit is proven by re-parsing and comparing the manifest data.
- **ignore_checksum**: it was never in the format-2 key lists; the unknown-key message now says what to do. The migration
  tool already drops it with a note (023). The 0.17 code still reads it (D13); its removal belongs to 029.

## Handoff

- 028: call `fetch_artifact(client, url, expected_sha256=manifest.source.sha256, skip=<decision or None>, sha256_from=...)` for a
  download source and `fetch_git(...)` for a git source; put `fetched.record.to_data()` in the receipt `[checksum]` table; `reverify`
  before unpack; catch `ChecksumError` and report `err.to_data()` with exit `err.exit_code`; for `check-updates`, catch `ChecksumMissing`
  and leave the version directory without `App` (D46). Never write `observed_sha256` into `pkg.toml`. Use `plan_checksum` for plans.
  In an elevated run give the client an admin-only cache directory.
- 029: `--skip-checksum[=reason]` through `parse_skip_flag`; `gupkg pkg pin <target>` reads the receipt's `[checksum]` with
  `parse_checksum_record` and calls `pin_hash`; show `unverified_note` in `list --details`, `info` and `check`.
- 030: use `gupkg.model.manager_policy`; the end-to-end manager matrix with a fake `gupkg` executable can then run against the real manager.
- 031: the registry tool publishes pinned hashes; no bypass exists.
