# Issue 020: gupkg 1.0 rehaul (epic)

Date: 2026-10-22
Priority: Critical
Change type: Architecture, package format, CLI, UI, registry. Breaking, with migration tools.

## Objective

Turn gupkg into a coherent, Scoop-like Python package manager for Windows.
The target design is in [`_docs/rehaul_design.md`](../_docs/rehaul_design.md),
which this issue tracks. Section numbers below (`§n`) refer to that document.

The owner's requirements and where each one is addressed:

| Requirement | Design | Issue |
| --- | --- | --- |
| Scoop-like, Python-based; one coherent CLI with no confusing manager mode | §10 | 029 |
| The manager is a **separate program** that knows how to call the CLI. There are two UI programs, `gupkg-tui` (one package) and `gupkg-manager` (general), with permanent `gupkg tui` and `gupkg manager` forwarders (D34) | §10.4, §15 | 029, 030 |
| First-class system-wide and per-user installation; the default mode is `auto` (elevated → system, otherwise user) | §6.1–6.2 | 025 |
| Third mode, **portable**: configuration stored inside the package folder | §6.3 | 025 |
| Better installer UI: key guide at the top, naturally scrolling output, coloured and emphasised phases, summary at the end | §14.2, §15 | 022, 030 |
| Standard, persistent installation logs: per package in `<root>\<name>\.gupkg\logs\`, with global run information in `<root>\.gupkg\logs\` (D26) | §14.3 | 022 |
| Clear manager behaviour for registry-only, local versions only, local tracker, and registry + local packages, installed or not. A local package follows the registry entry with the same name unless it opts out (D2) | §5 | 024 |
| First-class local packages | §5.4 | 024 |
| Recurring hook functionality becomes installer services | §8 | 026 |
| Stop relying on `_bootstrap`; versioned directories with an unpopulated `App` are the default distribution. A separate migration tool, outside core, converts 0.17 packages, trackers, config and installs (D33) | §4, §7, §17, §18 | 023, 024, 031 |
| `_bootstrap` renamed to **`_check_updates`**, a meta-package that discovers the newest version and creates a version directory, **without installing it**: installing is a separate operation. `gupkg check-updates` runs it (D21), and by default also downloads and populates the new version without activating it (D23) | §5.3, §7.4 | 024, 026, 028, 029 |
| Version directories do not get a checksum populated automatically; the CLI fails on a missing checksum unless a flag is given, and the flag works only for `https://` URLs (D24); the manager follows an `ask`/`refuse`/`skip` setting (default `ask`) | §9 | 027 |
| Version directories carry no update-discovery code; the update checker is the only component that discovers new versions (a security boundary). Other network use by version-directory scripts is allowed (D28) | §9.3 | 023, 026, 028 |
| Hard-to-test system installation becomes testable | §16 | 021 |
| A clear threat model for running as administrator (T1): an elevated gupkg never acts on a user root (D31), and elevated launchers recursively check everything they will run (D30); the registry is never a source of information; gupkg keeps its files only in its roots | §9.5–9.7 | 025, 033 |
| `App` is disposable when `[source]` has a `download_url` and is not marked `shipped = true`; otherwise it is shipped (the default without a `download_url`, for example proprietary software), a first-class kind that is never dropped. `shipped = true` with a `download_url` is valid, and the URL then only recovers a missing `App`. A custom populate script in `pkg.local\` is the other way to populate `App`, recoverable while its inputs exist (D27). Project URL, download URL and check URL are distinct keys | §4 | 023, 028 |
| Bundled mode: gupkg can travel inside another program's distribution (`<version>\gupkg\`) and install it **without installing gupkg**; package launchers use an installed gupkg unless `--no-system-gupkg=true` is given, and otherwise the bundled one, with no version comparison (D32); gupkg's own package always carries a bundled gupkg, which becomes its `App` | §21 | 033 |

## Workstreams and order

```text
stage 0 (0.18, no format change):
016 release ZIP ─► 018 one interpreter, 019 no pip ─┐
017 COM shortcuts/junctions ────────────────────────┤
                                                    │ all of stage 0 (D35)
stage 1+ (1.0):        ┌────────────────────────────┘
                       ▼
021 system boundary ──┐
022 events + logs ────┼─► 028 engine ─► 029 CLI + machine interface ─► 030 gupkg-tui, gupkg-manager
023 format 2 model,   │        ▲
    migration tool ───┤        │
024 catalog/state ────┤        │
025 install modes ────┤        │
026 services ─────────┴─► 027 checksum policy
031 registry 2 (after 023, 026, 027; uses 023's migration tool)
033 bundled mode, launchers, bootstrap (after 028, 029, 030)
032 docs, release (last; closes this epic)
```

| Issue | Title | Depends on |
| --- | --- | --- |
| 016 | One self-contained release ZIP (external, stage 0) | — |
| 017 | Shortcuts and junctions without PowerShell or `cmd` (external, stage 0) | — |
| 018 | Remove system-Python autodetection (external, stage 0) | 016 |
| 019 | The bundled Python ships without pip (external, stage 0) | 016 |
| 021 | System boundary, FakeSystem, and sandbox mode | 016, 017, 018, 019 (all of stage 0; D35) |
| 022 | Event stream, console renderer, and standard logs | 021 |
| 023 | `pkg.toml` format 2, settings model, and migration tool | 019 (for locking `tomli_w`) |
| 024 | Catalog and package state model | 023 |
| 025 | First-class install modes (system, user, portable) | 021, 023 |
| 026 | Built-in services replacing hook scripts | 021, 022, 023 |
| 027 | Checksum policy | 023, 026 |
| 028 | Execution engine: phases, receipts, uninstall, cleanup, check | 021, 022, 024, 025, 026, 027 |
| 029 | Unified command-line interface and machine interface | 028 |
| 030 | `gupkg-tui` and `gupkg-manager`: separate UI programs, and the UI redesign | 018, 022, 027, 029 |
| 031 | Registry 2: pinned versions, trackers, registry tool, package migration (with 023's migration tool) | 023, 026, 027 |
| 032 | Migration path, documentation, issue hygiene, 1.0 release | all |
| 033 | Bundled mode, package launchers, and bootstrap | 016, 023, 026, 028, 029, 030 |

Stage 0 can ship as 0.18 on its own, and 021 starts only after all of it
has landed (D35). Then 021 and 023 can proceed in parallel, and 022
follows 021. Each issue is meant to be mergeable on its own behind the
existing CLI until 029 switches the front end.

## Stage 0: the external issues 016–019

These were filed separately on 2026-10-22 and cover how gupkg itself is built,
shipped and started. Their decisions stand (D13). The analysis and the full
integration are in design §20, and each of these issues has an "Integration
with the 1.0 rehaul" section. In summary:

| Issue | What the rehaul relies on | What the rehaul adds or asks |
| --- | --- | --- |
| 016 | No runtime pip or dependency installs; `MANIFEST.sha256`; deletion of `dependencies.py`, `locked_deps.py` and `dependency_notices.py`. | The release ZIP is a distribution with a bundled gupkg (D15, issue 033). In 1.0, installing it copies the bundled tree into `App` through a custom populate script (D27), which removes the `App\RELEASE.txt` marker. In 1.0, its self-update hooks become a format 2 package with a custom `track` script, a custom populate script and `MANIFEST.sha256` checks (D14, issues 026 and 033). `self update` splits into `self check-updates` and `self update` (D11, D21, issue 029). 0.18 → 1.0 is a one-time bootstrap re-run, and 0.18's `self update` points to the bootstrap when it sees a 1.0 release (D33). |
| 017 | COM shortcuts with read-back. | Do the optional junction part too, because issue 021 needs it as a contract-tested `System` operation. Lands strictly before 021 (D35). |
| 018 | One interpreter, enforced on every start. | The same launcher rule for `gupkg-tui` and `gupkg-manager` (D34). Both run their sibling `gupkg.cmd`, never one found on `PATH`. |
| 019 | A pip-free bundle and a build probe. | Lock `tomli_w` (today it is only vendored in pip). The probe also imports both UI programs. |

## Decisions

The decisions are listed in §19 of the design. These are settled by the
owner (2026-10-22):

- **D1:** the tracker folder is named `_check_updates`.
- **D4:** `default_mode = "auto"`. An elevated run selects system, and a
  non-elevated run selects user. It never selects portable, and applies
  only when no flag, install record or containing root decides.
- **D10:** version directories contain no update-check code: no `[track]`
  and no `track` hook. D28 reduces it: other network use by
  version-directory scripts is allowed.
- **D11:** checking and installing are separate operations. `gupkg
  check-updates` (D21) writes new pinned version directories and stops
  without activating them (D23); `install` and `update` never run a
  tracker, and no flag combines the two.
- **D12:** the manager is a separate program, `gupkg-manager`. The CLI has no
  `--all`, no UI and no checksum prompts. Amended by D44: the separate
  programs may import gupkg core for reading, but every operation is a CLI
  call through the machine interface (`--json`, `--plan`, `--events-file`,
  `--no-input`).

These were added when the external issues were integrated:

- **D13:** stage 0 is issues 016–019, which land first and are built on
  rather than redesigned.
- **D14:** gupkg is an ordinary format 2 package of itself.
- **D15:** bundled mode. Any version directory may carry `gupkg\`, so a
  program can be installed without installing gupkg. A bundled install keeps
  and carries forward its copy, and never installs gupkg or writes a root
  forwarder. gupkg's own package always carries a bundled gupkg, and
  installing it copies that tree into `App` (through a custom populate
  script, D27).
- **D16:** package launchers find gupkg relative to themselves: the root
  forwarder `%~dp0..\..\.gupkg\gupkg.cmd`, then the bundled copy, and never
  `PATH`, the registry, AppData or ProgramData. Elevated launchers first
  verify that everything they will run is admin-only (PowerShell
  `Get-Acl`, fail-closed; recursive since D30) and refuse otherwise.
- **D17:** a distribution folder is copied into the mode's root before it is
  installed. System-wide installs of distributions are done only by the
  installed system gupkg from an admin shell. The system bootstrap extracts
  into an admin-only `C:\opt`.
- **D18:** the Windows registry is a write target, never a source of
  information.
- **D19:** threat model T1. A standard user must not gain administrator
  rights through gupkg: an elevated gupkg never runs code, and never reads
  inputs, that a standard user can modify.
- **D20:** gupkg owns no files outside its roots. Its data lives in
  `<root>\.gupkg\`, never in AppData or ProgramData.

The designer's proposals D2, D3 and D5–D9 were reviewed by the owner in
the review session (2026-10-22):

- **D2 (changed):** a local package follows the registry entry with the
  same name **automatically** (Scoop-like). `gupkg link --unlink` opts
  out, and `gupkg link` points it at another entry.
- **D5–D9:** confirmed as designed. D8 now means that the registry tool
  computes the hash when the vendor publishes none (`sha256_from =
  "registry"`), reviewed by the maintainer (D36).
- **D3** (a checksum mismatch is always fatal) is also the owner's own
  rule (job 07, owner decision 17), so it is settled.

These were added in the review session (2026-10-22). Where they differ
from D10, D11, D15 or D16 above, they win:

- **D21:** names. `gupkg check-updates` discovers updates (`gupkg self
  check-updates` for gupkg); `gupkg check [<target>…] [--fix]` is the
  health check and replaces `doctor`; `gupkg pkg check` stays the authoring
  validator; the manager's batch is `gupkg-manager check-updates --all`.
- **D22:** for a system package, an administrator runs `check-updates`
  elevated, and it writes straight into the system root. There are no
  proposals, no inbox and no `admit`. From a normal shell it implies
  `--no-download`, and for a system package it only reports. An elevated
  `check-updates` on a user package refuses.
- **D23:** by default `check-updates` writes the version directory,
  downloads and verifies the payload, and populates `App`; the version is
  ready but not active. `update` activates it and populates a version that
  has no `App` yet.
- **D24:** skipping a missing checksum is allowed for system installs too,
  but only for `https://` URLs. The run shows it, the receipt records it,
  and `check` flags it.
- **D25:** the CLI asks before consequential actions unless `--yes` is
  given; under `--no-input` a question is exit code 5. It never asks about
  checksums.
- **D26:** per-package logs in `<root>\<name>\.gupkg\logs\`; only global
  run information in `<root>\.gupkg\logs\`. `--events-file` takes a plain
  file name, written into those log directories. A bundled gupkg logs runs
  that involve no package into its own package's logs.
- **D27:** populate scripts are first class: the built-in download script
  or a custom populate script in `pkg.local\` (Python, `.ps1`,
  `.cmd`/`.bat` or an executable). `[source] local` is removed.
- **D28:** version-directory scripts and hooks may use the network, and
  every hook gets gupkg's shared HTTP client. Validation forbids only
  update discovery in a version directory. The `pkg check` import warning
  is dropped.
- **D29:** an install with no download is labelled *origin not verified*,
  unless the registry's fingerprint for that version matches (*verified
  against registry `<name>`*). A mismatch refuses the install.
  `verified-by-bootstrap` is dropped.
- **D30:** elevated launchers check recursively everything they will run,
  including through `current`.
- **D31:** an elevated gupkg never acts on a user root.
- **D32:** a launcher uses the installed gupkg behind the root forwarder
  unless `--no-system-gupkg=true` is given, otherwise the bundled copy,
  with no version comparison. Only `cleanup --bundled` removes bundled
  copies.
- **D33:** no legacy-specific code in core. A separate migration tool
  (issue 023) converts format 1, `_bootstrap`, the 0.17 manager config, and
  adopts 0.17 installs. 0.18 → 1.0 is a one-time bootstrap re-run.
- **D34:** two UI programs in issue 030, `gupkg-tui` and `gupkg-manager`,
  with permanent `gupkg tui` and `gupkg manager` forwarders. Neither
  imports gupkg core.
- **D35:** issue 017 lands strictly before 021, which depends on all of
  stage 0.
- **D36:** the registry tool only creates new version directories, with
  hashes and fingerprints, for a maintainer to review and push
  (`--auto-push` pushes directly). The registry keeps every version.
- **D37:** cancel in a UI stops after the current package.
- **D38:** the rule "no registry read except `PATH`" is guarded by a
  behaviour test. Its UI half was dropped by D44.
- **D39:** no `inno` kind in 1.0; a `tar` kind is added.
- **D40:** copying a distribution into a root removes the Mark of the Web.
- **D41:** every install records an `App` fingerprint by default
  (`record_manifest = false` opts out).
- **D42:** `--mode portable` on a folder shaped `<name>\<version>\pkg.toml`
  installs it in place.
- **D43:** `cleanup` never runs automatically unless `[cleanup] auto =
  true`; no lite bundle in 1.0.
- **D44:** `gupkg-tui` and `gupkg-manager` may import gupkg core and any
  libraries for reading and reference, but every operation they perform is
  a `gupkg` CLI call. The migration tool may import core and does its own
  work.

## Acceptance (epic)

- [ ] Every row of the requirements table is delivered and documented.
- [ ] Every one of the 79 registry packages installs with the strict CLI
      default (no `--skip-checksum`) in user mode, and the packages that
      declare `portable` also install in portable mode.
- [ ] At least 50 of the 62 registry hook files have been replaced by
      services; every hook that remains has a recorded reason.
- [ ] The default test suite exercises install, update, switch, uninstall
      and portable data persistence end to end against `FakeSystem`, with no
      patching of module attributes.
- [ ] A 0.17 machine (manager config, installed packages, `_bootstrap`
      templates) upgrades to 1.0 with the separate migration tool (issue
      023, D33), without reinstalling packages.
- [ ] A 0.18 install moves to 1.0 by re-running the bootstrap once (D33).
