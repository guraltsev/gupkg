# Issue 027: Checksum policy

Date: 2026-10-22
Priority: High (security)
Epic: 020. Design: `_docs/rehaul_design.md` §9

## Problem

### Checksums are inconsistent today

- A static `[origin]` without `checksum` is downloaded **unverified, with no
  warning** (`origin.py:167-173`). `--no-checksum` warns only when a checksum
  exists.
- Update payloads require a candidate `sha256`, unless `--no-checksum` or
  `ignore_checksum = true` is set.
  - 35 of the 79 registry manifests set `ignore_checksum = true`.
  - Some of them throw away GitHub digests that do exist: gsmartcontrol,
    keypirinha, nano, volta and wumgr (emacs also ignores checksums, but it is
  not on GitHub).
- **A required hash that is unavailable makes the update fail outright.**
  The 8 hooks that fetch digests optionally (dontsleep, firefox, gimp, julia,
  perl, portablegit, inkscape and bcu) must leave the policy at "required",
  so a missed scrape is a hard error rather than a choice for the operator.
- **`manager install` cannot skip checksums at all.** It has no
  `--no-checksum`.
- **Nothing is recorded.** No observed hash is stored anywhere, and there is
  no record of whether a download was verified.

### What the owner asked for

- The installation directory should not have a checksum populated
  automatically.
- The CLI should fail on a missing checksum unless a flag is given.
- The manager should follow an `ask` (default), `refuse` or `skip` setting in
  the gupkg settings file.
- The manager is a separate program that calls the CLI (decision D12).

## Scope

1. **The CLI rule (§9.1).** It applies wherever a download happens:
   `gupkg check-updates` (which downloads and verifies by default, D23),
   `install` and `update` (D24).
   - **A mismatch always fails.** No flag overrides it.
   - **A missing hash is refused**, unless the call carries
     `--skip-checksum`. That flag covers missing hashes only, and lasts for
     one invocation.
   - **`--skip-checksum` works only for `https://` URLs** (D24). A missing
     hash on any other URL is refused with or without the flag.
   - **System installs may skip too** (D24), including an elevated call
     that receives `--skip-checksum` relayed by the manager. The run shows
     the skip, the receipt records it, and `gupkg check` flags the install.
   - **The CLI never prompts about checksums** (D25), and it ignores the
     `[manager]` settings table.
2. **The plan exposes checksum status.** `gupkg install|update|check-updates
   … --plan --json` (issue 029) reports, for each target, `sha256: present |
   missing` and the source URL, so a caller can decide before anything is
   downloaded.
3. **The manager policy** (`gupkg-manager`, issue 030). The setting is
   `[manager] checksum_missing = "ask" | "refuse" | "skip"`, default `ask`.
   - **`ask`:** one prompt per package, showing the package, version, URL and
     where the URL came from. *Install once* runs that target with
     `--skip-checksum`; *Cancel* leaves it out. The answer is never stored.
   - **`skip`:** the manager runs every such target with `--skip-checksum`.
   - **`refuse`:** the manager reports these targets as
     "refused: no checksum" and does not run them.
   - **Nobody to answer** (non-interactive runs, `--yes`, no TTY): `ask`
     behaves as `refuse`.
4. **System-mode runs ignore the user settings layer** (issue 023). The
   CLI reads no checksum policy from any layer; a skip on a system install
   happens only through `--skip-checksum` on the call itself, given by the
   administrator or relayed by the manager to the elevated call (D24).
5. **The receipt** records:
   - `observed_sha256`;
   - `checksum_decision`: `verified`, `skipped-cli` (`--skip-checksum` given
     directly), `skipped-setting` (the manager's `skip`), or `user-approved`
     (the manager's `ask`, answered yes). There is no `verified-by-bootstrap`
     (D29). The manager passes the decision to the CLI as
     `--skip-checksum=<reason>`;
   - the URL, the final URL after redirects, and the size.

   `list --details`, `info` and `gupkg check` flag unverified installs.
   Installs with no download at all (a distribution folder, a custom
   populate script) are labelled by issue 028's origin rules (D27, D29),
   not by this decision field.
6. **Nothing writes a hash into `pkg.toml` automatically.**
   - `gupkg check-updates` copies only a hash the tracker *discovered*,
     through `render_pinned` (issue 023). The hash it observes when it
     downloads the payload goes into the receipt, not into `pkg.toml`.
   - `gupkg pkg pin <target>` is the explicit action that copies a receipt's
     observed hash into `pkg.toml`, with `sha256_from = "manual"`.
7. **`ignore_checksum` is removed** from the format. The separate
   migration tool (issue 023, D33) drops it and lists the packages that
   will now need a hash, or the policy.
8. **Git sources** must resolve to a commit, and the receipt records it.

## Acceptance criteria

- [ ] CLI, hash missing: `install` exits 3 with a message naming
      `--skip-checksum`. The download is never started, and the install
      record, `current` and the `FakeSystem` integration state are unchanged.
- [ ] CLI, hash missing, `--skip-checksum`, `https://` URL: the install
      succeeds and prints a warning, and the receipt says `skipped-cli`.
      The same holds for a system-mode install, and `gupkg check` then
      flags that install as unverified.
- [ ] CLI, hash missing, `--skip-checksum`, `http://` URL: the install is
      refused, nothing is downloaded, and nothing changes.
- [ ] `gupkg check-updates` on a found version with no checksum does not
      download it without `--skip-checksum`: it reports the version as
      needing `--skip-checksum` and leaves the version directory written
      without `App` (D46). With
      `--skip-checksum` (on an `https://` URL) it downloads and populates
      the version. A found version whose payload does not match its hash
      is reported as failed, and no ready `App` is left for it.
- [ ] Hash mismatch: the install fails with or without `--skip-checksum`, and
      under every manager policy (the manager part is verified once issue 030
      lands).
- [ ] Manager with `ask` (verified once issue 030 lands): one prompt per
      missing-hash package. *Cancel* leaves
      the package unchanged, and the batch continues with the next one.
- [ ] `gupkg-manager update --all --yes` with `ask` (verified once issue 030
      lands): packages without a hash are
      reported as "refused: no checksum", and the others proceed.
- [ ] A `checksum_missing = "skip"` in any settings layer has no effect on
      a CLI system-mode install that is not given `--skip-checksum`.
- [ ] A version directory written by `gupkg check-updates`, where the
      tracker found no checksum, has no `sha256` in its `pkg.toml`, also
      after a download with `--skip-checksum`.

## Tests

- **CLI behaviour matrix:** present, missing and mismatch × with and without
  `--skip-checksum` × `https://` and `http://` × `install` and
  `check-updates`, end to end against `FakeSystem`.
- **Manager policy matrix:** `ask`, `refuse` and `skip` × interactive and
  non-interactive, with a fake `gupkg` executable that records the argv it
  receives. This tests the manager through its real subprocess boundary.

Every case is labelled with an `id`.
