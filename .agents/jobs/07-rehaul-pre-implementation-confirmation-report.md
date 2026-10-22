# Job 07 report: rehaul pre-implementation confirmation

Date: 2026-10-22
Branch: `rehaul-design`. Commit 1 holds the design state as it was when
the job started. Commit 2 holds the consistency fixes, spikes and this
report. Commit 3 applies the owner's answers to every "Needs owner" item and
open question (see "Owner decisions from the review session"); where this
report's later sections describe the state after commit 2, the decisions
table supersedes them.

## Summary verdict

**Ready with conditions.**

The design and issues 016–033 now agree with the owner decisions. Every
`§n`, `Dn` and "issue NNN" reference resolves, and the declared dependency
graph has no cycle. The three assumptions that could force a redesign hold:

- **The elevated permission check works**, including under Constrained
  Language Mode, and every unsafe case fails closed. This was tested as a
  standard user only (S1).
- **Downloaded distributions are not blocked when run from a console.** A
  double-click on an Explorer-extracted `install.cmd` is held up by the Mark
  of the Web prompt, and unblocking the ZIP first fixes it (S2).
- **Installers can be extracted** with the bundled 7-Zip for six of the
  seven packages. Two kinds do not work this way: MSYS2's Zstandard
  `.sfx.exe` and GIMP's Inno Setup 7 installer (S3).

Conditions, each to be met before the issue named:

1. ~~**Before 025, 028, 029 and 033:** the owner answers the T1 questions
   N1–N5 and N7–N9.~~ Met: answered in the review session (see "Owner
   decisions from the review session").
2. ~~**Before 028:** the owner settles open question 4.~~ Met: D41.
3. **Before 033 merges:** S1 is re-run from an elevated shell, and S2 is
   repeated on a clean VM with a real Edge download and Smart App Control in
   evaluation mode. Neither was possible in this session (see "Spike
   results"). **Still open.**
4. ~~**Before 026 and 031:** the owner decides how the `inno` kind and
   MSYS2 are handled.~~ Met: D39.

## Owner decisions from the review session

The owner answered every "Needs owner" item and every open question on
2026-10-22, in a review session after commit 2. They are recorded in the
design as D21–D43 (§19) and applied to the design and issues 016–033 in
commit 3. Where an answer replaced an earlier answer in the same session,
only the final one is listed. "Reading" marks a detail that the designer
inferred from the owner's answer and the owner did not spell out.

| D | Decision | Answers |
| --- | --- | --- |
| D2 (changed) | A local package follows the registry entry with the same name **automatically** (Scoop-like). A package can opt out (`gupkg link --unlink`), or be pointed at another registry entry with `gupkg link`. | D2 |
| D5–D9 | Confirmed as designed. D8 now means "the registry tool computes the hash when the vendor publishes none" (`sha256_from = "registry"`), reviewed by the maintainer (D36). | D5–D9 |
| D21 | **Names.** Update discovery is `gupkg check-updates`; `gupkg self check-updates` is its alias for gupkg. `gupkg check [<target>…] [--fix]` is the health check and replaces `doctor`. `gupkg pkg check` stays the authoring validator. The manager's batch is `gupkg-manager check-updates --all`. | — |
| D22 | **Who runs `check-updates`.** For a system package an administrator runs it from an admin shell (elevated), and it writes straight into the system root. There are no proposals, no inbox and no `admit`. From a normal shell it is allowed for every package and implies `--no-download`; for a system package it then only reports. An elevated `check-updates` on a user package refuses (D31). | N1, N19 |
| D23 | **What `check-updates` writes.** By default it writes the new version directory, downloads and verifies the payload, and populates its `App`; the version is ready but not active. `update` (or `install name@ver`) then activates and integrates it, still as a separate operation (D11). `--no-download` writes only the `pkg.toml`-and-support-files directory where the root is writable, and only reports otherwise. `update` populates a version that has no `App` yet. | — |
| D24 | **Skipping a missing checksum** is allowed for system installs too, including when the manager passes `--skip-checksum` to the elevated call, but **only for `https://` URLs**. The run shows it, the receipt records it, and `check` flags the install. The checksum rules apply wherever a download happens (`check-updates`, `install`, `update`). | N2 |
| D25 | **CLI questions.** The CLI asks before consequential actions (`uninstall --purge`, multi-target plans, anything destructive) unless `--yes` is given; with `--no-input` a question becomes exit code 5. It never asks about checksums. | N3 |
| D26 | **Logs.** Per-package information goes to `<root>\<name>\.gupkg\logs\`; `<root>\.gupkg\logs\` holds only global run information, with links to the per-package logs. `--events-file NAME` takes a plain file name, not a path; the CLI writes the event stream into those log directories, and the caller reads it there. A bundled gupkg logs runs that involve no package into its own package's `.gupkg\logs\`. | N4, N12 |
| D27 | **Populate scripts are first class.** `[source]` names how `App` is populated: the built-in **download script** (`download_url`, optional `sha256`; it downloads, verifies, unpacks, then runs the package's remaining local install steps through a hook), or a **custom populate script** in `pkg.local\` (Python, `.ps1`, `.cmd`/`.bat` or an executable; non-Python scripts run as a subprocess and receive the context as JSON). A custom-populated `App` is **as verified as the package itself**, and is **recoverable while the script's inputs exist** (repair re-runs the script; `cleanup` drops it only then). `shipped` keeps its meaning: `App` is present and nothing populates it. `[source] local` is **removed**. | N6, N22 |
| D28 | **Network.** Scripts and hooks in a version directory **may use the network** (Python cannot sandbox trusted code). What D10 keeps, enforced by validation: no update-discovery code in a version directory (no `[track]`, no `track` hook). All hooks get gupkg's shared HTTP client in their context. The `pkg check` import warning is dropped. | (network question) |
| D29 | **Installs with no download** (a distribution folder) are labelled *origin not verified*, unless the registry publishes a **fingerprint** for that package and version (one SHA-256 over the version directory's files) and it matches: then *verified against registry `<name>`*. Compared at install when the synced registry is on disk, and again by `check`. A **mismatch refuses the install** (like a checksum mismatch) and `check` reports it for installed packages. `verified-by-bootstrap` is dropped. | N5 |
| D30 | **Elevated launchers check everything they will run, recursively:** root, package directory, version directory, `<root>\.gupkg\` and the forwarder, and the whole gupkg and Python tree that will start. Launched through `current`, the check follows the junction, requires its target to be a version directory of the same package, and checks both. `%SystemRoot%` is trusted to locate PowerShell; same-account malware is out of scope (T1 rule 5), and §9.6 says so. | N7, N8, N9 |
| D31 | **An elevated gupkg never acts on a user root**: neither through the `auto` fallback nor through an explicit `--mode user`. It refuses and says to use a normal shell. | N10 |
| D32 | **Which gupkg runs.** A launcher uses the installed gupkg reachable through the root forwarder, unless `--no-system-gupkg=true` is given; otherwise the package's bundled copy. No version comparison. Carry-forward copies the package's own bundled copy. Old version directories keep their bundled copies; only an explicit `cleanup --bundled` removes them, and only when an installed gupkg can take over. | N11, N13, Q5 |
| D33 | **No legacy-specific code in core.** The migration tool is a **separate, non-core program** (built in issue 023): format 1 → format 2, `_bootstrap` → `_check_updates`, the 0.17 manager config → settings, and **adoption** of 0.17 installs (writing their install records). Core has no 0.17 aliases, no format 1 hint and no `_bootstrap` handling (it is just a reserved `_` name). 0.18 → 1.0 is a one-time **bootstrap re-run**; 0.18's `self update` points to the bootstrap when it sees 1.0. | N14, N16, N17 |
| D34 | **Two UI programs, both in issue 030:** `gupkg-tui` (one package; `gupkg tui PATH` forwards to it; version directories carry `gupkg-tui.cmd`) and `gupkg-manager` (general; `gupkg manager …`, including `gupkg manager tui`, forwards to it). The forwarders are permanent. Both follow D12: no import of gupkg core, only the machine interface. | (TUI question) |
| D35 | Issue 017 lands **strictly before** 021; 021 depends on all of stage 0. | N15 |
| D36 | **Registry tool.** It only creates new version directories (with hashes and fingerprints); a maintainer reviews and pushes. `--auto-push` commits and pushes directly. The registry keeps **every** version; deletion is manual. | N18, Q2 |
| D37 | **Cancel** in a UI stops after the current package: the running CLI finishes it cleanly and skips the rest. An elevated call is asked to stop through a stop marker, which can only make it stop. | N20 |
| D38 | The rules "the UI programs import nothing from gupkg core" and "no registry read except `PATH`" are guarded by **behaviour tests**: the UIs run with only the CLI available, and installs are checked against `FakeSystem`'s registry access log. | N21 |
| D39 | **Unpack kinds.** `inno` is dropped for 1.0; GIMP keeps running its vendor installer. A `tar` kind (`.tar.xz`, `.tar.gz`, …, two 7-Zip passes) is added; MSYS2 uses its `.tar.xz`. | N23 |
| D40 | Copying a distribution into a root **removes the Mark of the Web** from the copies; the receipt records that the source was marked. | N24 |
| D41 | Every install records an **`App` fingerprint** (SHA-256 of every file) by default; a package can opt out with `record_manifest = false`; a global switch remains. | Q4 |
| D42 | **Portable target.** `--mode portable` on a folder already shaped `<name>\<version>\pkg.toml` installs it in place (the folder above `<name>` is the portable root). Otherwise the configured portable root is used, or gupkg asks (an error under `--no-input`). | Q1 |
| D43 | `cleanup` never runs automatically (`[cleanup] auto = true` enables it after `gupkg-manager update --all`). No lite bundle in 1.0. | Q3, Q6 |

Readings to keep in mind:

- **gupkg's own package (D27).** It has no `download_url` and no
  declarative tracker. Its `_check_updates` uses a custom `track` script,
  and its version directories use a custom populate script that copies the
  bundled `gupkg\` folder into `App`. When a version written by
  `check-updates` has no bundled folder yet, the populate script fetches
  the release itself (D28 allows it).
- **The download script's hook (D27).** The built-in download script runs
  the package's `[[prepare]]` steps after unpacking. A custom populate
  script can run the same steps through its context.

## Consistency fixes made

All of these were applied in commit 2. "OD" means the owner decision of the
same number in the job file.

### `_docs/rehaul_design.md`

| Section | Change | Justification |
| --- | --- | --- |
| Header | "issues 020–032 implement it" → "020–033". | Issue 033 exists and is part of the epic. |
| §1 | "63 hook files" → 62; "About 5,300 lines" → 5,200; "contradict each other in three packages" → "two packages (tixati and sqlitebrowser)". | Factual spot-checks W8–W10. |
| §3 | Vocabulary entry *Pinned version*: "has a concrete URL" → "is concrete: no templates, and either a fixed `download_url` … or a shipped `App`". | OD 14 (`shipped` with or without a `download_url`). |
| §5.3, rule 1 | Added: in a root with no root forwarder, the running bundled gupkg is carried into a copied-in registry version. | §21.4 (carry-forward on every new version directory); §21.7 cites §5.3 for it. |
| §5.4 | The mode order for path targets was record → root → `--mode` (required). It now refers to §6.2: `--mode`, record, root, then `auto`. | OD 2 (D4). |
| §5.4 | "A package installed from a path outside every configured root" → "installed in place (`--in-place`)", kept in `state.toml`. | OD 10 (D17: copy first); §11 (`state.toml`). |
| §5.4 | `gupkg new` → `gupkg pkg new`. | §10.2 grammar. |
| §6.2 and the D4 row | Removed the fallback "elevated, package without system → user". Such a package is now refused, with a hint to run from a non-elevated shell. | OD 2 ("an elevated process selects system"); OD 12 and §9.6 rule 2 (an elevated gupkg ignores user roots). The owner is asked to confirm under N10. |
| §7.4, step 3 | Added the carry-forward of the bundled gupkg and the launchers. | §21.4; §21.7 cites §7.4 for it. |
| §8.2 | The `inno` kind notes that innoextract 1.9 reads Inno Setup only up to 6.1. Added that `sfx` covers only codecs the bundled 7-Zip supports. | Spike S3. |
| §9.2 | Added `verified-by-bootstrap` to the `checksum_decision` values. | §21.6 uses it. Its semantics are N5. |
| §9.4 | `_docs/elevated-runtime-design.md` → `docs/elevated-runtime-design.md` (issue 032 moves it). | Factual W15: only `docs/` exists. |
| §10.1 | Root-only options now include `--sandbox DIR`. | §16 uses `gupkg --sandbox DIR …`. |
| §10.2 | Added `cleanup --bundled`, `gupkg shell`, `pkg check --install` and `pkg bundle --lite` (open question 6). The `cleanup` comment now says "old Apps", not "old versions". | Used in §21.8, §6.3, §16 and §21.7. §13.3 keeps version directories. |
| §12, §15 | `gupkg-manager update --all [--mode M] [--yes]`. | §10.3 and issue 030 already use `--mode`. |
| §13.1 | Resolve phase: "in both CLI and UI" → the manager groups system targets into one call. | OD 5 (D12: only the CLI elevates, by relaunching itself). |
| §14.2 | The example was `gupkg install firefox` with "local tracker → upstream". It is now `gupkg update firefox` from a known local version. | OD 4 (D11: install never runs a tracker). |
| §19, D12 row | "no UI and no prompts" → "no UI and no checksum prompts". | OD 5, verbatim. |
| §20.1, row 018 | The elevated-relaunch references "sections 6.2, 13.2" → "9.6, 13.1". | Those are the sections that describe it. |
| §21.3 | "How it is checked": the script parses `.Sddl` and calls no .NET methods (Constrained Language Mode). It travels inside the launcher, because `-EncodedCommand` exceeds `cmd`'s line limit. A null DACL, an unreadable ACL, and an unknown ACE type or right fail. | Spike S1. These are direct consequences of the results, not a scope change. |
| §21.6 | `self check` for a system root writes a proposal. | §9.3 rule 6 and OD 4. |

### Issues

| Issue | Change | Justification |
| --- | --- | --- |
| 016 | Line references: `gupkg.cmd:60-91`, `:25-28, 104-120`, `dependencies.py:205-223` and `gupkg-bootstrap.ps1:95-136`. | Factual W1–W4. |
| 016 (integration section) | "issue 032 replaces them" → 033; `url` → `download_url`; self update split into `self check` and `self update`; the bootstrap is thin (issue 033) and installing gupkg writes the settings. | OD 4, 7, 15; design §20.2 item 6, §21.5. Only the "Integration with the 1.0 rehaul" section was touched. |
| 018 | Line references `gupkg.cmd:60-64`, `:93-102` and `elevation.py:127-168`. In the integration section, the manager runs the `gupkg.cmd` of its own tree (`$App\gupkg.cmd` or `<version>\gupkg\gupkg.cmd`). | Factual W5–W7; design §10.4. |
| 020 (epic) | Requirements row for `App`: `shipped = true` with a `download_url` is valid. | OD 14. |
| 020 | Dependency table: 022 ← 021; 026 ← 022; 030 ← 027; 033 ← 030. Diagram and order text updated to match. | Every issue's own text needs these (021 `System.run`, the reporter in `HookContext`, the manager policy, `gupkg-manager.cmd`). |
| 020 | "issues 026 and 032" → 026 and 033. | Issue 033 converts gupkg's own package (033 scope 8, 032:64). |
| 020 | D4, D10, D11, D12 and D15 summaries completed with the owner wording. "D13–D20 stand unless overruled" → only D2, D3 and D5–D9 are unconfirmed. "never registers gupkg" → "never installs gupkg or writes a root forwarder". | OD 2–5 and 8; job statement on D2, D3, D5–D9. |
| 020, 026, 031 | Hook count 63 → 62, about 5,300 → 5,200 lines. | Factual W11. |
| 021 | "about 15 times" → 12. The grep criterion is limited to `--type py`, so it does not match 033's `.cmd` launcher. The `on_line` callback is connected by 022 rather than 021. | Factual W16; dependency fix. |
| 022 | Log criterion: `<root>\.gupkg\logs\`, nothing in AppData or ProgramData. The `manager update` criterion becomes the CLI multi-target run. `test_line_stream.py` is deleted, not rewritten. | OD 13; D12; `_docs/tests.md` (no tests of deleted internals). |
| 023 | "in 5 places" → 4. The `shipped` criterion is now validation-level and includes the exact error text. The `project_url` criterion is now validation-level (the HTTP client is not in 023). The settings criterion covers elevated/system runs, `--settings` and AppData. Layer 2 is `$App\settings.toml`. "provider tables per issue 026" reworded. | Factual W12; OD 14, 13, 15; design §11. |
| 024 | Four criteria rewritten at catalog level: `_check_updates` (not `_bootstrap`); refresh-or-keep of a copied tracker; ad-hoc roots via `state.toml`; no `gupkg-manager` in a 024 criterion. | OD 1, 5; design §5.3 rule 2; acceptance cycles. |
| 025 | Scope: "a portable root is never the system root". The T1 criterion is now testable. The `auto` criterion is at resolution level, refuses an elevated package without `system`, and never picks `portable`. Four end-to-end criteria moved to 028. | OD 2, 12, 18; design §6.3; cycle C3. |
| 026 | `_tools` → `tools/`; `pkg check` qualified (029). "No hook performs HTTP" replaced with a behavioural criterion. Added a `verify_manifest` criterion. | `_docs/tests.md`; design §8.2, §9.3. |
| 027 | Emacs is not on GitHub. Added `verified-by-bootstrap`. "Nothing written outside `.gupkg\work`" replaced (logs are always written). Manager criteria marked "verified once issue 030 lands". | Factual W17; design §14.3, §21.6; cycle C4. |
| 028 | The disposable-`App` criterion is limited to versions with a `download_url`; the `local`/bundled part moved to 033. The purge confirmation follows §4 and `--no-input`. Received the four install-mode criteria from 025. | OD 14; cycles C2, C3. |
| 029 | Exit code 6 added. The §10.3 criterion distinguishes CLI rows from manager rows. `C:\opt\vscode` (a system-root path) → `%USERPROFILE%\opt\vscode`. Elevated criterion requires the system gupkg. `check`, `update` and `--plan` criteria made exact. The `rg input(` grep is replaced by a `--no-input` behaviour test. Added "`update --all` and `update --check` are usage errors". | Design §10.1, §21.7; OD 4, 5, 12; `_docs/tests.md`. |
| 030 | The manager runs the `gupkg.cmd` of its own tree. The launcher is delivered by 033 (no back-dependency). The log location is given. "One UAC prompt" made testable and T1-correct. Proposals for system packages. The 2,000-line criterion uses Pilot instead of "not re-rendering". | Design §10.4, §9.3, §9.6; OD 12; `_docs/tests.md`. |
| 031 | "never run a tracker" → only URLs written in the version's `pkg.toml` are fetched, under `--sandbox`. Portable-settings packages lose settings "whatever `only_portable` says". | OD 3, 4 (testable form); factual W13. |
| 032 | The bootstrap neither writes settings nor runs `settings init`. | Design §20.2 item 6, §21.5. |
| 033 | Factual W14 (AppData config search never existed). `local` schema attribution. The self-managing criterion made testable. Mark of the Web criterion records S2. The uninstall criterion covers packages without a bundle. Added criteria for D17 system installs and for recoverable bundled installs (from 028). | Factual W14; OD 10; spike S2. |

Several replaced passages had made long lines; they were reflowed to the
surrounding width. `git diff --check` passes.

### Searches used for superseded terms

These were run over `_docs/rehaul_design.md` and `_issues/issue016-*` to
`issue033-*` (`grep -nE`):

- `Software\\gupkg|\[\[winreg\]\]|locator file|registration`
- `LOCALAPPDATA|%APPDATA%|ProgramData%\\gupkg|APPDATA%\\gupkg`
- `robocopy`
- `into = "gupkg"`
- `gupkg ui|update --check|update --all|--all`
- `homepage|\[source\] url|\[track\] url|`url``
- `no flag`
- `_latest`
- `_bootstrap`
- `on .?PATH|from .?PATH|search(es)? .?PATH|found on`
- `gupkg (ui|tui)|manager mode`
- `shipped`

After the fixes, every remaining hit falls into one of these groups:

- **0.17 descriptions:** design §1; issues 016:47/149, 023, 024 and 031.
- **Rejected alternatives:** the "Alternatives considered" column of §19 (D1, D15–D18, D20).
- **Migration text:** design §4, §10.2 `pkg migrate`, §12 `migrate/` and §18.
- **Windows integration targets that D20 allows:** the Start Menu under `%APPDATA%` and `%ProgramData%`.
- **Prohibitions:** for example, "creates nothing under `%LOCALAPPDATA%`".
- **The manager's own `--all`.**
- **Ordinary English uses of "shipped".**
- **021:72:** a throwaway `HKCU\Software\gupkg-test-<uuid>` test key, which is a write target.

### Reference check

A script collected every `§n`, `§n.m` and `section(s) n` reference, every
`Dn` and every `issue(s) NNN` in the design and issues 016–033. It checked
511 references against the design's headings (sections 1–21 and their
subsections), the decision table (D1–D20) and `_issues/` (including
`_closed/`). **None is broken.**

## Needs owner

Each item is a question with options and a recommendation. Items N1–N9
touch the threat model (T1) and should be answered first.

**N1. How does an elevated `update` find a system package's proposal?**

- **The conflict.**
  - §9.3 rule 6 puts proposals in `%USERPROFILE%\opt\.gupkg\proposals\`.
  - §9.6 rule 2 says the elevated side "ignores user settings, user roots
    and the user's environment" and accepts only a name or a path inside
    the system root.
  - With over-the-shoulder elevation, the elevated `%USERPROFILE%` is the
    administrator's, not the user's.
- **The options:**
  - **(a)** `gupkg update <name> --proposal <path>`, an explicit argument
    that is treated as data.
  - **(b)** Let an administrator run `check` for system packages elevated.
    The tracker code is admin-only, and network responses are data. "Check
    never runs elevated" is designer text, not an owner decision.
  - **(c)** System packages update only from linked-registry pinned
    versions.
- **Recommendation:** (b). It needs no user-writable hand-off at all.

**N2. Where is `[manager] checksum_missing` enforced for system targets?**

- **The problem.** §9.4 claims a user cannot set `skip` to affect a system
  install. But the non-elevated manager reads the user layer and passes
  `--skip-checksum` to the elevated call. §9.6 rule 2 does not say whether
  flags from the non-elevated side are accepted.
- **The options:**
  - **(a)** The manager reads the policy only from the system layer for
    system targets.
  - **(b)** The elevated child rejects `--skip-checksum` from a non-elevated
    parent.
  - **(c)** Missing checksums are always refused for system mode.
- **Recommendation:** (a). §9.4's sentence should then name it.

**N3. Does the CLI prompt at all?**

- **The conflict.** §10.1 says every mutating command takes `--yes`. §4
  has a purge confirmation and §9.3 an admission confirmation. 029 scope 4
  keeps confirmations. But §13.2 says "the CLI holds no confirmation
  logic". OD 5 forbids only checksum prompts.
- **The options:**
  - **(a)** The CLI confirms destructive actions unless `--yes`, and
    `--no-input` turns any prompt into exit 5.
  - **(b)** The CLI never prompts. Destructive actions need `--yes`, and
    otherwise exit 5 with the plan.
- **Recommendation:** (a), and reword §13.2 to "no checksum or batch
  confirmation logic".

**N4. Who chooses the `--events-file` path that an elevated child writes?**

- **The problem.** The non-elevated parent chooses it, which conflicts with
  §9.6 rule 2.
- **The options:**
  - **(a)** The child writes only under `<system root>\.gupkg\logs\`, and
    the parent passes a run id.
  - **(b)** Allow a user path, created exclusively and without following
    reparse points.
- **Recommendation:** (a).

**N5. What does gupkg record for an `App` populated from `local`, or from a
distribution folder?**

- **The problem.** `verified-by-bootstrap` (§21.6) assumes gupkg knows the
  ZIP was verified, but `install.cmd` cannot tell a bootstrap run from a
  manual unzip. `MANIFEST.sha256` ships inside the same ZIP, so it proves
  consistency, not origin. D17 distribution installs have no checksum at
  all.
- **The options:**
  - **(a)** A generic `local-manifest` decision ("no download; tree matches
    its own manifest").
  - **(b)** The bootstrap passes the verified ZIP hash to `install.cmd`, and
    gupkg records it as provenance.
  - **(c)** Both: (a) by default, (b) when given.
- **Recommendation:** (c), with `list --details` showing such installs as
  unverified-origin.

**N6. Is a verified `local` folder enough to populate a version that has no
`download_url`?**

- **The conflict.** §21.2 says a `local` without a `download_url` counts as
  shipped. §4 says a shipped version with an empty `App` fails with
  `App is missing and no way to recover it.` Read literally, a `local`-only
  first install fails.
- **The options:**
  - **(a)** A present, verified `local` folder is a recovery source.
  - **(b)** `local` requires a `download_url`.
- **Recommendation:** (a).

**N7. How much must an elevated launcher check?** (S1)

- **The current rule.** OD 9 and D16 say "root and own version directory".
  The design adds that "everything below inherits".
- **What S1 found.**
  - On real Windows an admin-only parent can have a user-writable child:
    `C:\Windows` passes while `C:\Windows\Tasks` grants Authenticated Users
    write.
  - The check never covers `<root>\.gupkg\gupkg.cmd`, which the launcher
    executes.
  - It never covers the package root, whose delete-child right would let a
    user replace a version directory.
  - It never covers `python.exe` and the standard library, which run before
    gupkg can check its own tree.
- **The options:**
  - **(a)** Keep two directories, and accept that an administrator's
    explicit ACL changes are out of scope (T1 rule 5).
  - **(b)** Also check the package root, `.gupkg\` and the forwarder file.
  - **(c)** Check recursively, every object on the execution path.
    `icacls C:\opt\gupkg /save /T` over 1,655 items took 0.3–0.4 s here,
    plus about 0.6 s of PowerShell start-up per check.
- **Recommendation:** (b), plus the bundled or installed `gupkg\` tree
  non-recursively.

**N8. Launchers run through the `current` junction.**

- **The problem.** For `…\current\install.cmd`, `%~dp0` is a junction.
  `Get-Acl` returns the junction's own ACL, not the target's (S1, case 6).
  The spike check refuses reparse points.
- **The options:**
  - **(a)** Refuse, and document that elevated launchers run from the real
    version directory. Shortcuts already point at real paths.
  - **(b)** Resolve the junction target in the check, and check both.
- **Recommendation:** (b) if users are expected to run
  `current\gupkg-manager.cmd` elevated. 033's own acceptance uses
  `current\gupkg-manager.cmd`, non-elevated.

**N9. Can an elevated launcher trust `%SystemRoot%`?**

- **The problem.** The PowerShell path comes from an environment variable.
  A per-user `HKCU\Environment` override of `windir` or `SystemRoot` is a
  known UAC-bypass vector for same-account elevation.
- **The options:**
  - **(a)** Accept it, because T1 rule 5 excludes malware running as the
    user that requests UAC.
  - **(b)** Derive the Windows directory without the environment.
- **Recommendation:** (a), recorded explicitly in §9.6.

**N10. Confirm the D4 change made under "Consistency fixes".**

- **The change.** An elevated `auto` run on a package without `system` is
  refused rather than installed in user mode.
- **A related question:** may an elevated gupkg act on a user root at all
  with an explicit `--mode user`? §9.6 rule 2 implies no.

**N11. Do old version directories keep their bundled copies?**

- **The conflict.** §21.8 says `cleanup` removes old bundled copies "along
  with the old versions", but §13.3 keeps every version directory.
  Dropping a non-current version's bundle breaks its launchers after
  `switch` back.
- **The options:**
  - **(a)** Keep them; only `cleanup --bundled` touches bundles.
  - **(b)** `cleanup` drops bundles of non-current versions and `switch`
    re-carries one.
- **Recommendation:** (a).

**N12. Logs for a bundled gupkg that acts on no root.**

- **The conflict.** §14.3 writes nothing. OD 19 says "every run writes
  logs".
- **The options:**
  - **(a)** Use the default user root's `.gupkg\logs\`, which bundled
    installs already create (§21.4).
  - **(b)** Accept the exception.

**N13. Carry-forward when the running gupkg is not the package's own
bundle.**

- **The example.** A user-mode installed gupkg in another root manages a
  forwarder-less root.
- **The options:**
  - **(a)** Copy the package's current bundle.
  - **(b)** Copy the running gupkg.
  - **(c)** Write a forwarder (§21.3 already does this for a "non-elevated
    installed gupkg managing a user root").
- **Note:** (c) conflicts with OD 8 only for bundled installs.

**N14. Remaining acceptance-induced cycles.**

- **The cycle.** 031 uses `gupkg pkg migrate` (032), and 032 depends on
  "all".
- **The options:**
  - **(a)** Split `pkg migrate` into an early slice of 032.
  - **(b)** Move it into 023, which already keeps the 0.17 reader.
- **What was already done.** The other cycles found (024, 025, 027, 028,
  029 ↔ later issues) were removed by moving or annotating criteria, as
  listed above.

**N15. Must 017 land before 021?**

- **The conflict.** The epic says 021 depends on "017 (preferred)", and 017
  and 021 describe 017 landing after 021. OD 6 says stage 0 lands first.
- **The options:**
  - **(a)** 021 depends on 016–019, and the conditional text is deleted.
  - **(b)** Keep "preferred".
- **Note:** this touches 017's order text, which this job may not change
  without you.

**N16. The 0.18 → 1.0 self-update hop.**

- **The problem.** 016:208 and 032 assume the 1.0 engine performs the first
  update, but 0.18's hooks will.
- **The options:**
  - **(a)** The 1.0 ZIP stays installable by the 0.18 hooks, and 1.0
    converts on first run.
  - **(b)** 0.18 → 1.0 requires the bootstrap.

**N17. What does 1.0 do with a leftover `_bootstrap`?**

- **The options:**
  - ignore it silently;
  - ignore it and warn with a `pkg migrate` hint;
  - refuse the package.
- **Recommendation:** warn.

**N18. Does registry CI commit directly or open a pull request?**

- **The conflict.** §9.3 rule 4 says it "turns it into a pull request".
  031 scope opens a PR only for `review = true` or changed hook code.

**N19. What does an elevated `gupkg check` do?**

- **The options** (028 scope says "drops elevation, or refuses with a
  hint"):
  - drop elevation;
  - refuse with exit 2.
- **Dependency:** this follows from N1.

**N20. Manager cancel granularity.**

- **The conflict.** §15 says "cancel after the current step?". 030 says
  "after the current package", and the manager sends every user target in
  one CLI call.
- **The options:**
  - **(a)** The manager signals the CLI, which stops between phases or
    packages.
  - **(b)** Cancel only between CLI calls.

**N21. Module-boundary tests versus `_docs/tests.md`.**

- **The conflict.** "`gupkg_manager` imports nothing from `gupkg`" (D12)
  and "registry reads only in `system/windows.py`" (§9.5) are mandated as
  tests. `_docs/tests.md` discourages tests of implementation structure.
- **The options:**
  - **(a)** A documented exception in `tests.md`.
  - **(b)** A CI lint step instead of a test.

**N22. What happens to the `.ps1` origin scripts in 1.0?**

- **The conflict.** 017 keeps them in `origin.py`, but 028 deletes
  `origin.populate_*`, and format 2 has no `.ps1` kind.
- **The options:**
  - drop them;
  - add a `[[prepare]] module` equivalent;
  - add a `ps1` kind.

**N23. Unpack kinds that the bundled 7-Zip cannot handle.** (S3)

- **`inno`.** innoextract 1.9 (zlib licence, last release 2020) reads Inno
  Setup only up to 6.1. Current GIMP is built with Inno Setup 7.0, and
  innoextract fails on it.
  - **(a)** Evaluate innounp 2.71 (licence unclear: GPL on its page, MIT on
    GitHub).
  - **(b)** Keep `[source.installer] run` for GIMP, and leave `inno` for old
    installers only.
  - **(c)** Drop `inno` from 1.0.
- **MSYS2.** Its `.sfx.exe` uses Zstandard, which mainline 7-Zip lacks.
  - **(a)** Use the release's `.tar.xz`. This needs a `tar` / `tar.xz` kind
    (two 7-Zip passes), which §8.2 does not list.
  - **(b)** Bundle 7-Zip ZS.
  - **(c)** Run the SFX as a vendor installer.
- **Recommendation:** (b) for `inno`, and (a) for MSYS2.
- **Knock-on:** issue 026's acceptance criterion that msys2 is "expressible
  with no hook" depends on this.

**N24. Mark of the Web on copied distributions.** (S2)

- **The problem.** Explorer extraction marks every file. D17 then copies
  the marked tree into the root, so its launchers keep prompting on every
  double-click.
- **The options:**
  - **(a)** The copy into a root strips `Zone.Identifier` after the admin
    or user chose to install it.
  - **(b)** Keep the marks and document `Unblock-File`.
- **Recommendation:** (a), recorded in the receipt.

**N25. Designer decisions D2, D3 and D5–D9.**

- **The finding:** no problems with them. D9 ("`update` never installs a
  missing package") is consistent with the §5.3 table.
- **Status:** they remain unconfirmed by the owner.

## Spike results

### Environment

- **Machine:** Windows 11 Enterprise 26H2, build 26300.9711. The registry
  `ProductName` reads "Windows 10 Enterprise", as usual.
- **PowerShell:** Windows PowerShell 5.1.26100.9711.
- **Account:** a local administrator account running a filtered token at
  medium integrity: `BUILTIN\Administrators` is deny-only. This is the
  "standard user" context.
- **No elevated context was available.** The session was non-interactive,
  and a UAC prompt could not be answered.
- **Smart App Control:** off (`VerifiedAndReputablePolicyState = 0`).
- **`C:\`** on this machine has no "Authenticated Users: create folders"
  entry, so a standard user cannot squat `C:\opt` here. The squatted case
  was reproduced with the default-Windows ACL in a temporary folder.
- **`C:\opt`** exists, holds real software, and is admin-only
  (`O:BA`, `D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)`).
  It was only read.

### S1: the elevated permission check

**Files** (in `.agents/jobs/07-spikes/s1/`):

- `acl-check.ps1`: the check;
- `launcher-template.cmd`: the launcher;
- `run-s1.ps1`: the harness;
- `results-standard-user.txt`: the full output, with SIDs and paths
  anonymised.

**Command:**
`powershell -NoProfile -ExecutionPolicy Bypass -File run-s1.ps1 -WorkDir <temp>`.

**How the check works:**

- **Inputs.** The launcher passes `GUPKG_ACL_PATHS=%~dp0..\..|%~dp0.`.
- **How it runs PowerShell.** It runs
  `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -Command "Invoke-Expression $env:GUPKG_PSCMD"`.
  That command reads the script from the launcher itself (`%~f0`), after a
  marker line.
- **What it checks.** The script parses `(Get-Acl).Sddl`. The owner must be
  BA, SY or TrustedInstaller. No allow ACE for another SID may carry write
  data, append, write EA, delete child, write attributes, delete, WRITE_DAC,
  WRITE_OWNER, GENERIC_ALL/WRITE or MAXIMUM_ALLOWED. An inherit-only
  CREATOR OWNER entry is accepted.
- **When the launcher proceeds.** Only when the output is exactly one line,
  `GUPKG-ACL-OK`.
- **Elevation detection.** `whoami /groups | find "S-1-16-12288"`.

**Results.** Every row behaved the same under `full`, `clm` and
`clm-envvar`. In both `ps-missing` and `ps-wrong-exe`, every row refused.

| Case | Expected | Result |
| --- | --- | --- |
| 1. admin-only `C:\opt` + its gupkg version directory | proceed | PROCEED |
| 2. version directory with a user granted write | refuse | REFUSE (owner, and `BU` 0x1301bf) |
| 2b. admin-owned `C:\ProgramData` (`BU` has `DCLCRPCR`) | refuse | REFUSE |
| 2c. `C:\Windows\Tasks` (`AU` 0x1200ab) | refuse | REFUSE |
| 2d. `C:\Windows` + `System32` (sibling `Tasks` is user-writable) | proceed | PROCEED (see N7) |
| 3. squatted root (user-owned, `AU` Modify) | refuse | REFUSE |
| 4. Downloads | refuse | REFUSE |
| 5. null DACL | refuse | REFUSE ("null DACL") |
| 6. junction to `C:\opt` | refuse | REFUSE (reparse point; see N8) |
| 7. `…\..\..` path | proceed | PROCEED |
| 8. missing directory | refuse | REFUSE |

**The variants:**

- **`full`:** normal PowerShell.
- **`clm`:** Constrained Language Mode, set in the session before the
  check is compiled. A probe confirmed that a method call such as
  `[IO.File]::Exists` is blocked.
- **`clm-envvar`:** `__PSLockdownPolicy=4` in the process environment.
  Windows PowerShell 5.1 ignores it, and the probe reported
  `FullLanguage`. It only honours the machine-level variable, which needs
  administrator rights to set.
- **`ps-missing`:** PowerShell path does not exist (blocked). No output →
  refuse in about 0.1 s.
- **`ps-wrong-exe`:** a different program runs instead of PowerShell. No
  `OK` line → refuse.

**Other findings:**

- **Constrained Language Mode.** The `.Sddl` approach works there.
  `GetAccessRules(...)`, which the job suggested, is a method call, so CLM
  would block it and the check would then fail closed (refuse) on every
  machine under WDAC or AppLocker. The `icacls /save` fallback is therefore
  not needed.
  - It was not exercised: `icacls /save` was only timed (0.3–0.4 s over
    1,655 items of `C:\opt\gupkg`).
  - A real WDAC or AppLocker policy was not available. The CLM result comes
    from setting the session language mode.
- **Null DACL.** `Get-Acl` renders a null DACL as an SDDL with no `D:`
  part. .NET's `GetAccessRules` synthesises an `Everyone: FullControl`
  rule, so neither approach fails open.
- **`-EncodedCommand`.** The encoded script (about 12,000 characters) is
  over `cmd`'s 8,191-character limit. Parentheses on the `for /f` line end
  the clause, so the command travels in an environment variable.
- **Cost.** About 0.55–0.8 s per launch, almost all of it PowerShell
  start-up.
- **No case failed open.**
- **Not run:** any case from an elevated shell, and the true branch of
  elevation detection. The check's logic does not depend on elevation,
  because reading an ACL needs only READ_CONTROL. The script can be re-run
  as is from an admin shell.

**Design impact:**

- §21.3 updated: `.Sddl` parsing, the script carried in the launcher, and
  the fail-closed cases.
- N7, N8 and N9 go to the owner.

### S2: Mark of the Web

**Files:** `.agents/jobs/07-spikes/s2/run-s2.ps1` and `results.txt`.

**What was built.** A 23.8 MB ZIP with
`myapp\1.0\{install.cmd, gupkg\python\…, App\dummy.txt}`. Its `python\` is
the committed `src/gupkg/python/`. `install.cmd` runs the bundled
`python.exe -I -s -B` and writes a marker file.

**How the download was simulated.** The script writes the `Zone.Identifier`
stream Edge writes (`ZoneId=3`, `ReferrerUrl`, `HostUrl`). Edge itself could
not be driven from this session.

| ZIP | Extraction | Files marked | `install.cmd` from `cmd` | Double-click (ShellExecute) |
| --- | --- | --- | --- | --- |
| marked | Explorer (Shell.Application `CopyHere`) | all | runs | **does not start within 20 s** (consistent with the "Open File – Security Warning" prompt) |
| marked | `tar -xf` | none | runs | runs |
| `Unblock-File` before extraction | Explorer | none | runs | runs |
| `Unblock-File` before extraction | `tar -xf` | none | runs | runs |

**What it shows:**

- **Execution.** Marked `python.exe` and DLLs run without any prompt when
  started from `cmd`. This is how `install.cmd` chains to gupkg, and how
  the bootstrap script runs `install.cmd`. Nothing was blocked.
- **The fixes.** Running `Unblock-File` on the ZIP before extraction, or a
  recursive `Unblock-File` afterwards, clears every mark.
- **Smart App Control** is off here and cannot be re-enabled without
  reinstalling Windows. Its on and evaluation states, and SmartScreen
  toggling, were not tested.

**Design impact:**

- 033's Mark of the Web criterion now records these results.
- N24 goes to the owner.

### S3: installer extraction

**Files:** `.agents/jobs/07-spikes/s3/` holds `results.md` (full table,
commands and the 0.17 comparison), `download.sh`, `extract.ps1` and
`extract_followup.ps1`.

**Setup:**

- **Tool:** the bundled `7z.exe` (7-Zip 26.03 x64). Nothing downloaded was
  executed.
- **Integrity:** every vendor checksum that exists matched.

| Package | Kind | `7z x` usable? | Needed |
| --- | --- | --- | --- |
| firefox 158.0.1 | 7z SFX | yes | `core\` |
| obsidian 1.14.4 | NSIS | yes | nested `$PLUGINSDIR\app-64.7z` |
| pidgin 2.14.14 | NSIS | yes | merge nested `$PLUGINSDIR\gtk.zip` |
| qbittorrent 5.3.0 | NSIS | yes | exclude `*.pdb` (270 MB), `uninst.exe` and `$PLUGINSDIR` |
| zotero 10.0.6 | 7z SFX (Mozilla-style) | yes | `core\` |
| gimp 3.2.6 | Inno Setup 7.0 | **no**: only PE resources | — |
| msys2 2026-09-27 `.sfx.exe` | 7-Zip ZS SFX, Zstandard | **no**: "Unsupported Method" | the release's `.tar.xz` works in two 7z passes |

**innoextract:**

- **Licence:** zlib, so it can be redistributed. Ship its `license/`
  folder too.
- **Builds:** official Windows build 1.9 (2020).
- **Coverage:** Inno Setup ≤ 6.1.
- **On GIMP:** it fails with "Could not determine setup data version!".

**Other notes:**

- SourceForge `/download` links return HTML with HTTP 200 to browser-like
  User-Agents, so a checksum or magic-byte check is needed before
  extracting.
- A 7z exit code of 0 can carry warnings, so `require` checks are still
  needed.
- In 0.17, pidgin's hook deletes `pidgin-uninst.exe`, but 7z names the file
  `pidgin-uninst.exe.nsis`.

**Design impact:**

- §8.2 updated (`inno` coverage, `sfx` codecs).
- N23 goes to the owner.

## Open-question memos

The design's §19 lists six open questions. None is decided here.

**Q1. The default portable root.**

- **Options:**
  - `%USERPROFILE%\opt-portable`;
  - prompt on first use.
- **Trade-offs.** A fixed default is scriptable and matches the
  `[roots]` table. A prompt conflicts with `--no-input` and with the CLI
  having no prompts beyond confirmations (N3). Portable is never chosen by
  `auto` (OD 2), so a default only matters for `--mode portable` without a
  path.
- **Recommendation:** the fixed default, overridable in `[roots]`.

**Q2. How many pinned versions the registry keeps (N = 2).**

- **Options:**
  - 1 (smallest);
  - 2 (one rollback step);
  - N configurable per package.
- **Trade-offs.** Each version is a small `pkg.toml`, so size is not the
  concern. Review load in registry PRs grows with N. Rollback for linked
  installs needs an old version that still has a valid `download_url`, and
  vendors delete old artifacts.
- **Recommendation:** 2, with a per-package override for vendors that keep
  only the latest artifact.

**Q3. Whether `cleanup` runs after `gupkg-manager update --all`.**

- **Options:**
  - off, with `[cleanup] auto = true`;
  - on by default.
- **Trade-offs.** On by default saves space (S3: qbittorrent's unfiltered
  payload is 309 MiB, msys2's 13,834 files). But it removes the fast
  rollback `App` right after an update, which is when rollback is most
  likely. It also interacts with N11 (bundled copies).
- **Recommendation:** off by default, as proposed.

**Q4. Whether every install records an `App` manifest. This must be
settled before issue 028 starts.** It decides the receipt format and the
Prepare and Finish phases of the engine.

- **Options:**
  - on by default, with `[doctor] record_manifests = false`;
  - off by default, opt-in;
  - only for `shipped` and `local` Apps, where `doctor` has no other way to
    detect modification.
- **Measured cost.** SHA-256 over trees extracted in S3, first read after
  extraction, on this machine with its real-time antivirus:

  | Tree | Files | Size | Time |
  | --- | --- | --- | --- |
  | firefox | 75 | 356 MiB | 1.2 s |
  | obsidian | 83 | 387 MiB | 2.2 s |
  | zotero | 87 | 242 MiB | 1.4 s |
  | pidgin | 746 | 58 MiB | 9.5 s |
  | msys2 | 13,834 | 225 MiB | 116 s |

  Cost is driven by file count (per-file open overhead), not by size.
- **Trade-offs.** Detecting modified files (supply chain, damaged disks)
  versus up to two minutes added to an msys2-sized install. Hashing could
  run during staging, where files are already being written. That needs a
  measurement inside the 028 executor.
- **Recommendation:** on for `shipped` and `local` Apps and for gupkg
  itself. For recoverable Apps, on by default with a per-package opt-out
  (`[source] record_manifest = false`) for very large trees. The global
  switch stays.

**Q5. Whether `cleanup --bundled` runs by default when a root has a
forwarder to a newer installed gupkg.**

- **Options:**
  - off (as proposed);
  - on during `cleanup`.
- **Trade-offs.**
  - About 40 MB per version directory (S2's bundle alone is 56 MB
    uncompressed for `python\`).
  - Dropping bundles makes those packages depend on the installed gupkg,
    and on its forwarder staying in place.
  - It also interacts with N11.
- **Recommendation:** off by default, and `doctor` reports the reclaimable
  space.

**Q6. Whether to offer a lite bundle (`pkg bundle --lite`).**

- **Options:**
  - no;
  - yes, without `gupkg_manager` and Textual.
- **Trade-offs.**
  - The S2 ZIP with only `python\` was 23.8 MB, so Textual and Rich are a
    minority of the size.
  - A lite bundle's `gupkg-manager.cmd` loses the package view.
  - It adds a second bundle shape to test and sign.
- **Recommendation:** no for 1.0. Revisit when size complaints appear.

**New open questions** from this review are N1–N24 above. Of those, N1–N5
and N7–N9 are prerequisites for issues 025, 028, 029 and 033. N23 is a
prerequisite for 026 and 031.

## Factual spot-checks

70 claims were verified against the 0.17 sources and the registry clone
(79 manifests, commit `b339183`). The tables below list the ones that were
wrong and a representative selection of the correct ones.

**Wrong or partly wrong (all corrected):**

| # | Claim (where) | Checked against | Verdict |
| --- | --- | --- | --- |
| W1 | `gupkg.cmd:63-91` (016) | `src/gupkg/gupkg.cmd` (`:ordinary_launch` at 60) | partly; now 60-91 |
| W2 | `gupkg.cmd:21-24, 103-123` (016) | URL and SHA at 25–28, download at 104–120 | wrong; corrected |
| W3 | `dependencies.py:205-230` (016) | function ends at 223 | partly; corrected |
| W4 | `gupkg-bootstrap.ps1:94-140` (016) | `Install-BundledRuntime` at 95–136 | partly; corrected |
| W5 | `elevation.py:125-160` (018) | `enforce_elevated_runtime` at 127–168 | wrong; corrected |
| W6, W7 | `gupkg.cmd:59-64`, `93-101` (018) | 60–64, 93–102 | minor; corrected |
| W8 | "contradict each other in three packages" (design §1) | tomllib over the registry: tixati and sqlitebrowser | wrong; now two |
| W9–W11 | "63 hook files", "about 5,300 lines" (design §1, §18; 020; 026; 031) | `find */pkg.local -name '*.py'`: 62 files, 5,213 lines | wrong; corrected |
| W12 | `startswith("bootstrap")` "in 5 places" (023) | grep: `gupkg.py:394, 599, 663`, `updates.py:380` | wrong; now 4 |
| W13 | "declare `only_portable = false`" (031) | tixati and SumatraPDF-portable declare `true` | wrong; corrected ("whatever `only_portable` says") |
| W14 | 0.17 config search in `%APPDATA%` (033) | `manager.py:191-201` reads only beside `manager.py` | wrong; corrected |
| W15 | `_docs/elevated-runtime-design.md` (design §9.4) | only `docs/` exists | wrong; corrected |
| W16 | `install_components` patched "about 15 times" (021) | 12 patch sites in `tests/` | partly; corrected |
| W17 | emacs throws away GitHub digests (027) | emacs is fetched from `ftp.gnu.org` | partly; corrected |

**Correct (selection):**

| # | Claim | Checked against |
| --- | --- | --- |
| C1 | 35 of 79 manifests set `ignore_checksum` (design §1, 027, 031) | tomllib over the registry: `79 35`, all in `[update.payload]` |
| C2 | 78 of 79 packages are `_bootstrap`; qbittorrent ships `5.2.3/` (031) | registry listing |
| C5 | 5 byte-identical redirect resolvers (design §1, 026) | md5 of `_resolve` in the 5 hooks |
| C6 | 6 hand-rolled GitHub clients (design §8.1) | grep `api.github.com` in hooks |
| C35 | `current` is linked before population, `gupkg.py:208-240` (design §4) | junction at 216, population at 229 |
| C47 | `_bootstrap` sorts newest (design §1) | `compare_package_versions('_bootstrap','99.0') == 1` |
| C55 | the only registry read is the PATH read-modify-write (design §9.5) | `read_registry_value` used only at `components.py:370` |
| C56 | exit codes 0/2/3/4 (design §10.1) | `core.py:34-37` |
| C57 | `commands.py` is 1,190 lines (design §12) | `wc -l` |
| C58 | `tomli_w` is only vendored inside pip (design §12) | not in the lock; present under `pip/_vendor` |
| C61 | `package-tui.cmd` searches `PATH` (design §21.3, 033) | `src/package-tui.cmd:12-16` |

**Not verified** (they need code reading or upstream knowledge rather than
grep):

- "9 near-identical" scrapers (only the 9 files were confirmed to exist);
- "13 ways of finding a checksum";
- "5 version comparators";
- "several ignore `appReady`";
- the 300–800 ms per shortcut in 017;
- "`auto` means four different things".
