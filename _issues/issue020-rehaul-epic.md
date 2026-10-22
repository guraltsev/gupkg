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
| The manager is a **separate program** that knows how to call the CLI | §10.4, §15 | 029, 030 |
| First-class system-wide and per-user installation; the default mode is `auto` (elevated → system, otherwise user) | §6.1–6.2 | 025 |
| Third mode, **portable**: configuration stored inside the package folder | §6.3 | 025 |
| Better installer UI: key guide at the top, naturally scrolling output, coloured and emphasised phases, summary at the end | §14.2, §15 | 022, 030 |
| Standard, persistent installation logs | §14.3 | 022 |
| Clear manager behaviour for registry-only, local versions only, local tracker, and registry + local packages, installed or not | §5 | 024 |
| First-class local packages | §5.4 | 024 |
| Recurring hook functionality becomes installer services | §8 | 026 |
| Stop relying on `_bootstrap`; versioned directories with an unpopulated `App` are the default distribution | §4, §7, §17 | 023, 024, 031 |
| `_bootstrap` renamed to **`_check_updates`**, a meta-package that discovers the newest version and creates a version directory, **without installing it**: installing is a separate operation | §5.3, §7.4 | 024, 026, 028, 029 |
| Version directories do not get a checksum populated automatically; the CLI fails on a missing checksum unless a flag is given; the manager follows an `ask`/`refuse`/`skip` setting (default `ask`) | §9 | 027 |
| Version directories carry no update-check code; the update checker is the only component that accesses the network to discover anything (a security boundary) | §9.3 | 023, 026, 028 |
| Hard-to-test system installation becomes testable | §16 | 021 |
| A clear threat model for running as administrator (T1); the registry is never a source of information; gupkg keeps its files only in its roots | §9.5–9.7 | 025, 033 |
| `App` is disposable when `[source]` has a `download_url` and is not marked `shipped = true`; otherwise it is shipped (the default without a `download_url`, for example proprietary software), a first-class kind that is never dropped. `shipped = true` with a `download_url` is valid, and the URL then only recovers a missing `App`. Project URL, download URL and check URL are distinct keys | §4 | 023, 028 |
| Bundled mode: gupkg can travel inside another program's distribution (`<version>\gupkg\`) and install it **without installing gupkg**; package launchers prefer an installed gupkg and fall back to the bundled one; gupkg's own package always carries a bundled gupkg, which becomes its `App` | §21 | 033 |

## Workstreams and order

```text
stage 0 (0.18, no format change):
017 COM shortcuts/junctions ──────────────┐
016 release ZIP ─► 018 one interpreter, 019 no pip
                                          │
stage 1+ (1.0):                           ▼
021 system boundary ──┐
022 events + logs ────┼─► 028 engine ─► 029 CLI + machine interface ─► 030 gupkg-manager
023 format 2 model ───┤        ▲
024 catalog/state ────┤        │
025 install modes ────┤        │
026 services ─────────┴─► 027 checksum policy
031 registry 2 (after 023, 026, 027)
033 bundled mode, launchers, bootstrap (after 028, 029, 030)
032 migration, docs, release (last; closes this epic)
```

| Issue | Title | Depends on |
| --- | --- | --- |
| 016 | One self-contained release ZIP (external, stage 0) | — |
| 017 | Shortcuts and junctions without PowerShell or `cmd` (external, stage 0) | — |
| 018 | Remove system-Python autodetection (external, stage 0) | 016 |
| 019 | The bundled Python ships without pip (external, stage 0) | 016 |
| 021 | System boundary, FakeSystem, and sandbox mode | 017 (preferred) |
| 022 | Event stream, console renderer, and standard logs | 021 |
| 023 | `pkg.toml` format 2 and settings model | 019 (for locking `tomli_w`) |
| 024 | Catalog and package state model | 023 |
| 025 | First-class install modes (system, user, portable) | 021, 023 |
| 026 | Built-in services replacing hook scripts | 021, 022, 023 |
| 027 | Checksum policy | 023, 026 |
| 028 | Execution engine: phases, receipts, uninstall, cleanup, doctor | 021, 022, 024, 025, 026, 027 |
| 029 | Unified command-line interface | 028 |
| 030 | `gupkg-manager`: separate manager program and UI redesign | 018, 022, 027, 029 |
| 031 | Registry 2: pinned versions, trackers, CI autoupdate, package migration | 023, 026, 027 |
| 032 | Migration tools, documentation, issue hygiene, 1.0 release | all |
| 033 | Bundled mode, package launchers, and bootstrap | 016, 023, 026, 028, 029, 030 |

Stage 0 can ship as 0.18 on its own. After it, 021 and 023 can proceed in
parallel, and 022 follows 021. Each issue is meant to be mergeable on its own
behind the existing CLI until 029 switches the front end.

## Stage 0: the external issues 016–019

These were filed separately on 2026-10-22 and cover how gupkg itself is built,
shipped and started. Their decisions stand (D13). The analysis and the full
integration are in design §20, and each of these issues has an "Integration
with the 1.0 rehaul" section. In summary:

| Issue | What the rehaul relies on | What the rehaul adds or asks |
| --- | --- | --- |
| 016 | No runtime pip or dependency installs; `MANIFEST.sha256`; deletion of `dependencies.py`, `locked_deps.py` and `dependency_notices.py`. | The release ZIP is a distribution with a bundled gupkg (D15, issue 033). In 1.0, installing it copies the bundled tree into `App` (`local = "gupkg"`), which removes the `App\RELEASE.txt` marker. In 1.0, its self-update hooks become a format 2 tracker plus the new `verify_manifest` service (D14, issues 026 and 033). `self update` splits into `self check` and `self update` (D11, issue 029). |
| 017 | COM shortcuts with read-back. | Do the optional junction part too, because issue 021 needs it as a contract-tested `System` operation. Land before 021. |
| 018 | One interpreter, enforced on every start. | The same launcher rule for `gupkg-manager`. The manager runs its sibling `gupkg.cmd`, never one found on `PATH`. |
| 019 | A pip-free bundle and a build probe. | Lock `tomli_w` (today it is only vendored in pip). The probe also imports `gupkg_manager`. |

## Decisions

The decisions are listed in §19 of the design. These are settled by the
owner (2026-10-22):

- **D1:** the tracker folder is named `_check_updates`.
- **D4:** `default_mode = "auto"`. An elevated run selects system, and a
  non-elevated run selects user. It never selects portable, and applies
  only when no flag, install record or containing root decides.
- **D10:** version directories contain no update-check code: no `[track]`,
  no check hook, and only the fixed URLs written in their `pkg.toml`.
- **D11:** checking and installing are separate operations. `gupkg check`
  writes new pinned version directories (or system-root proposals) and
  stops; `install` and `update` never run a tracker, and no flag combines
  the two.
- **D12:** the manager is a separate program, `gupkg-manager`. It imports
  nothing from gupkg and drives the CLI only through its machine interface
  (`--json`, `--plan`, `--events-file`, `--no-input`). The CLI has no `--all`,
  no UI and no checksum prompts.

These were added when the external issues were integrated:

- **D13:** stage 0 is issues 016–019, which land first and are built on
  rather than redesigned.
- **D14:** gupkg is an ordinary format 2 package of itself.
- **D15:** bundled mode. Any version directory may carry `gupkg\`, so a
  program can be installed without installing gupkg. A bundled install keeps
  and carries forward its copy, and never installs gupkg or writes a root
  forwarder. gupkg's own package always carries a bundled gupkg, and
  installing it copies that tree into `App` (`local = "gupkg"`).
- **D16:** package launchers find gupkg relative to themselves: the root
  forwarder `%~dp0..\..\.gupkg\gupkg.cmd`, then the bundled copy, and never
  `PATH`, the registry, AppData or ProgramData. Elevated launchers first
  verify that the location is admin-only (PowerShell `Get-Acl`, fail-closed)
  and refuse otherwise.
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

D2, D3 and D5–D9 were proposed by the designer and are not yet confirmed
by the owner. They stand unless they are overruled here before the
dependent issue starts.

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
      templates) upgrades to 1.0 with `gupkg settings init --from` and
      `gupkg pkg migrate`, without reinstalling packages.
