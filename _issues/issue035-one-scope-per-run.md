# Issue 035: One scope per run. Separate system and user modes, portable packages as their own kind, no migration code

Date: 2026-10-26
Priority: High
Builds on: issue 034 (keep it open as background; this issue carries its required behaviour and replaces its open questions)
Design touched: `_docs/rehaul_design.md` §3, §4, §5.3 rule 5, §5.4, §6 (all), §9.4, §9.6, §10.1–10.4, §11, §12, §13.2, §15, §18, §19, §21.3–21.8; `_docs/elevated-runtime-design.md`
Decisions amended or removed: D4, D16, D17, D20, D22, D24, D31, D32, D33, D37, D42, D44, D45

> **Status:** Open. This is a redesign that removes code. Phase 0 (owner decisions and the design note) comes before any code
> change. Read "Rules for this work" first.

## 1. Objective

The owner's requirements, 2026-10-26:

1. **Portable and non-portable packages are completely separate and unrelated.** A package is one or the other. No package
   definition, `App`, receipt or code path serves both.
2. **System use and user use of gupkg are completely decoupled.** A gupkg run at system level knows nothing about any user,
   and a run at user level knows nothing about the system installation. No run tracks or bridges both sides.
3. **All migration code is discarded.** That covers the migration tool, the readers of old layouts in core, and the hints
   about 0.17 spellings.
4. **The goal is a large simplification of the codebase with no loss of functionality.** Where this issue removes a
   capability, it says so and names its replacement, or it lists the removal as a decision for the owner.

Issue 034 asked the same thing for system and user and stopped at open questions. This issue answers them:

| Issue 034 question | Answer |
| --- | --- |
| Is portable a third mode, a variant of user mode, or removed? | **None of these.** Portable is a kind of package, not a mode or a mode. A portable package installs in user mode like any other package (§4.6). |
| Does a normal user's manager show the system packages read-only? | **No.** The owner's wording rules it out: the user mode is "completely unaware" of the system mode (§4.1). |
| What stays of `--mode` and `default_mode`? | **Nothing.** The mode is fixed when the process starts, from its token (§4.1). `--mode`, `default_mode`, the `system:`/`user:`/`portable:` selector prefixes and the relaunch all go. |
| Root forwarder: removed, or turned into a setting? | **Replaced by a setting.** A one-line data file in `<root>\.gupkg\`, not a script. A launcher tries the bundled copy, then that setting, then `PATH` (§4.9; owner's answer, 2026-10-26). |

## 2. State at the time of writing (2026-10-26)

What is committed, what is installed, and what is on the owner's machine. Everything here was checked, read-only. Nothing was changed.

- **Repository:** `main` is clean at `ff25e42`. Issue 034's section "State of the working tree" is **stale**. Its uncommitted
  edits were committed:
  - `638a384`: the manager's `a` key and the `plans.py` change;
  - `3270152`: `create_junction` and `tests/test_junction_replace_while_running.py`. Issue 034 recorded that this test
    passes with and without the fix. That has not been re-checked.
- **Installed gupkg:** 1.0.8, in `C:\opt\gupkg` (`C:\opt\gupkg\current\App\gupkg.cmd --version` → `gupkg 1.0.8`).
- **System root `C:\opt`:**
  - 97 package folders. 68 have an install record (`.gupkg\package.toml`).
  - 17 still have 0.17-style version folders (`v<version>.l<n>`).
  - 14 installed packages declare `modes = ["system", "user", "portable"]`: KeePassXC, Keypirinha, copyq, crystaldiskinfo,
    crystaldiskmark, hwinfo, notepadplusplus, obsstudio, rufus, serviwin, systeminformer, vscode, winmerge, winscp.
  - 6 have `[user]`/`[system]` step tables: KeePassXC, Keypirinha, copyq, mogan, notepadplusplus, systeminformer.
  - `C:\opt\.gupkg\` holds only `logs\`. It has no `settings.toml`, no `state.toml`, no `registry\` and no root forwarder.
- **User root `C:\Users\guraltsev\opt`:**
  - 16 package folders. **None** has an install record, and **all 16** are 0.17-style (for example
    `CopyQ-portable\v7.0.0.l2`).
  - Most of them are `*-portable` names installed as ordinary user packages.
  - `.gupkg\` holds `logs\` and `registry\`. It has no settings and no forwarder.
- **Registry clone `gupkg-registry` at `07a1140`:** 164 `pkg.toml` files.
  - `modes`: 130 `["system", "user"]`, 28 `["system", "user", "portable"]`, 6 `["portable"]`.
  - Keys of `[portable]` in use: `persist` 32, `markers` 10, `args` 0, `env` 0, `prepare` 0. `$Data` is used 0 times.
  - `[user]`/`[system]` steps appear in 6 packages. In 5 of them (keepassxc, keypirinha, mogan, notepadplusplus,
    systeminformer) the two tables are identical: they remove the portable switch of the vendor archive. copyq has only
    `[user] prepare = [["create", "unins000.exe"]]` and no `[system]` table. That is the one step in the registry that
    really differs by mode.
  - `PORTABLE-UNVERIFIED.md`: no portable declaration was ever tested against the real application.

## 3. Where the code mixes what must be separate

Line numbers are at `ff25e42`. Of the 37,700 lines of first-party `src`, the mode and elevation machinery below is spread over
about 50 files (`cli.py` alone has 84 elevation references, `modes.py` 45, `catalog.py` 40).

### 3.1 System and user in one run

1. **Mode selection has four inputs and a fallback** ([model/mode.py:193-294](../src/gupkg/model/mode.py#L193-L294)): the flag
   or selector prefix, the install record, the containing root, and `default_mode` with `auto`. An elevated run refuses
   non-system values from each of them in turn, so the same rule is written three times
   ([:245-251](../src/gupkg/model/mode.py#L245-L251), [:269-273](../src/gupkg/model/mode.py#L269-L273),
   [:285-289](../src/gupkg/model/mode.py#L285-L289)).
2. **`resolve_mode` names every root to refuse most of them** ([modes.py:118-254](../src/gupkg/modes.py#L118-L254)).
   - It expands all three configured roots ([:165](../src/gupkg/modes.py#L165)).
   - In an elevated run it still classifies a path against the user and portable roots, only to refuse it
     ([:309-327](../src/gupkg/modes.py#L309-L327)).
   - A normal run planning a system target reloads the settings "as the system would" and compares the roots of the two
     loads ([:238-244](../src/gupkg/modes.py#L238-L244)).
3. **One catalog for every root, with an elevated filter on top.**
   - `Catalog.from_settings` ([catalog.py:318-369](../src/gupkg/catalog/catalog.py#L318-L369)) adds every configured root plus
     the ad-hoc roots from each root's `state.toml`.
   - `_trusted_roots` ([:702-720](../src/gupkg/catalog/catalog.py#L702-L720)) then drops the ones an elevated run must not read.
   - `_view` adds problems for records of the wrong mode ([:757-760](../src/gupkg/catalog/catalog.py#L757-L760)).
   - `_require_system_path` refuses paths again ([:917-932](../src/gupkg/catalog/catalog.py#L917-L932)).
   - `AmbiguousSelector` exists only because one name can live in several roots
     ([:81-88](../src/gupkg/catalog/catalog.py#L81-L88)).
4. **The registry cache falls back across modes.** A user-mode package joins the *system* root's registry cache when the
   user's own was never synced ([catalog.py:792-815](../src/gupkg/catalog/catalog.py#L792-L815): `"user": ("user", "system")`).
5. **Shadowing warnings compare the two modes on purpose:**
   - [catalog.py:438-448](../src/gupkg/catalog/catalog.py#L438-L448);
   - [modes.py:462-484](../src/gupkg/modes.py#L462-L484).
6. **The settings of one run come from both modes.**
   - A user run reads the system root's `settings.toml` as a layer
     ([settingsload.py:206-226](../src/gupkg/settingsload.py#L206-L226)).
   - The system layer may move the roots, so the user layer's location is known only after the system layer has been read.
   - `_check_roots` requires all three roots to be distinct even though a run uses one
     ([:96-109](../src/gupkg/settingsload.py#L96-L109)).
7. **The CLI relaunches itself elevated from a user run.**
   - The elevation step is `relaunch.py` (165 lines).
   - [cli.py:320-376](../src/gupkg/cli.py#L320-L376) splits targets into a user group and a system group.
   - `_child_argv` ([:592-645](../src/gupkg/cli.py#L592-L645)) rebuilds a command line for the elevated child.
   - The hidden options `--relaunched`, `--batch-confirmed` and `--stop-file` exist only for that child.
   - `child-no-result`, `child_raw_exit_code` and `mixed-modes` ([lifecycle.py:205-236](../src/gupkg/engine/lifecycle.py#L205-L236))
     report on it.
   - `ShellExecuteEx`/`runas` is in `system/windows.py`, and its double is in `system/fake.py` (31 references).
   - The unsafe-character rules of design §10.4 exist mostly to protect that command line.
8. **The manager is a user program that acts in system mode through that child.**
   - `gupkg_ui/steps.py` (163 lines) groups targets into one normal call and one elevated call.
   - `can_elevate` and `NEEDS_SYSTEM_GUPKG` decide what a manager may ask for.
   - `UiSettings.checksum_policy(mode)` judges system targets "as an elevated run would"
     ([gupkg_ui/settings.py:50-57](../src/gupkg_ui/settings.py#L50-L57)).
   - `build_environment` refuses an elevated shell ([startup.py:117-119](../src/gupkg_ui/startup.py#L117-L119)), and so do
     the `tui`/`manager` forwarders ([cli.py:1186-1192](../src/gupkg/cli.py#L1186-L1192)).
9. **Commands that write a root layer branch on the mode.**
   - `registry sync|add|remove` refuses the system root from a normal shell ([cli.py:820-822](../src/gupkg/cli.py#L820-L822)).
   - `settings init|set` does the same ([:937](../src/gupkg/cli.py#L937)).
   - `_context` rebuilds a mode context for a command that has no package ([:791-812](../src/gupkg/cli.py#L791-L812)).
10. **Root forwarders bridge the modes by design.** A user root's forwarder may point to the system gupkg (design §21.3).
11. **Ad-hoc roots** (`--in-place`, portable in-place, `[[adhoc]]` in `state.toml`, `engine/stateio.py`, `Root.adhoc`,
    `Root.single`) add roots that a run did not configure, and each of them needs its own trust decision.

### 3.2 Portable and non-portable in one package

1. **`modes` mixes two axes.** `MODES = ("system", "user", "portable")`
   ([manifest.py:53](../src/gupkg/model/manifest.py#L53)) treats "where the package is installed" and "where the app keeps
   its state" as values of one axis. 28 registry packages declare all three.
2. **The steps differ per mode.**
   - `[user]`, `[system]` and `[portable].prepare` tables
     ([manifest.py:868-869](../src/gupkg/model/manifest.py#L868-L869), [:1314](../src/gupkg/model/manifest.py#L1314),
     [:1589](../src/gupkg/model/manifest.py#L1589)).
   - The receipt records which mode an `App` was prepared for (`[app] prepared_for`, `mode_signature`).
   - `_guard_the_app` ([lifecycle.py:640](../src/gupkg/engine/lifecycle.py#L640)) asks before rebuilding an `App`
     that was prepared for another mode, or refuses.
3. **A portable root is a third root.**
   - It comes with its own settings layer (`LAYER_PORTABLE`) and `[roots].portable`.
   - Its root is chosen by D42 ([modes.py:374-403](../src/gupkg/modes.py#L374-L403)): an in-place folder, else the
     configured root, else a question.
   - `_check_portable_root` ([:406-431](../src/gupkg/modes.py#L406-L431)) keeps it out of the other two roots.
   - `[integration.portable] integrate` is read only from that layer.
4. **`$Data` is valid only "when portable is in modes"** ([manifest.py:765](../src/gupkg/model/manifest.py#L765)). The
   registry never uses it.

### 3.3 Migration and legacy code

| Where | What | Size |
| --- | --- | --- |
| `src/gupkg_migrate/` | the migration tool (package, settings, adopt, shortcuts, hooks, cli) | 1,616 lines |
| `src/gupkg/gupkg-migrate.cmd`, [bootstrap.py:76-81](../src/gupkg/bootstrap.py#L76-L81), [cli.py:72](../src/gupkg/cli.py#L72) and its help line | the `gupkg migrate` forwarder | |
| [tools/build_standalone.py:67-70, 201, 221](../tools/build_standalone.py#L67) | build probe and launcher list | |
| [engine/receipts.py:57-70, 108-126](../src/gupkg/engine/receipts.py#L57-L70) | reads receipts at the 1.0.0–1.0.x path and moves them | ~30 lines |
| [engine/effects.py:54-62, 724-735](../src/gupkg/engine/effects.py#L54-L62) | deletes loose shim DLLs left in `bin` by older gupkg | ~25 lines |
| [model/manifest.py:863-867](../src/gupkg/model/manifest.py#L863-L867) | the special error for `[nonportable]` | 5 lines |
| [cli.py:1221-1249](../src/gupkg/cli.py#L1221-L1249) | `_REMOVED_OPTIONS` and `_Removed`: usage errors for 0.17 spellings | ~30 lines |
| `tests/test_migrate_*.py`, `tests/test_core_without_migration_tool.py` | the tool's tests | 1,234 lines |
| `tests/fixtures/legacy_017`, `legacy_examples`, `registry_017` | 0.17 inputs (476 KB). `registry_017` is also read by `test_catalog_registry.py`, `test_registry_100.py`, `test_registry_validate.py:155` and `test_service_hints.py` | |
| Docs | design §18 and the 0.17 rows of §10.3; migration text in `README.md`, `src/gupkg/README.md`, `_docs/{README,cookbook,operations,troubleshooting,development_guide}.md`; "0.18 moves to 1.0" in `gupkg-bootstrap.ps1:23` | |

`services/hints.py` **stays**. `gupkg pkg check` uses it, not only the migration tool.

## 4. Target design

Each rule below replaces a section of `rehaul_design.md`. The design note of phase 0 turns them into text there.

### 4.1 A run belongs to exactly one scope

- **Scope** = *(mode, root)*. The mode is `system` or `user`. The root is one folder. `Scope` is one frozen
  object, built **once, at process start**, before any settings file or package is read. Every subsystem receives it.
  Nothing builds another one.
- **The mode comes from the process token, and from nothing else.** An elevated process runs in system mode. Any other
  process runs in user mode, for the user who runs it. There is no `--mode`, no `default_mode`, no selector prefix, no relaunch
  and no "auto".
- **Names in the code.** `Scope` is the run's object; `mode` is its `system`/`user` field. Today's
  `system.port.Scope` enum (`AUTO`, `USER`, `MACHINE`) already is that distinction under the old name, so it becomes
  `Mode` (`SYSTEM = "system"`, `USER = "user"`; `AUTO` goes). Its values do not change, so effect records, which
  store `scope = "system"|"user"`, stay as they are on disk. `ScopeFolders` keeps its name. `Mode` also
  replaces the string constants `MODE_SYSTEM` and `MODE_USER`. The `pkg.toml` key `modes` keeps its name (Q5).
- **The root of the run is found by a fixed chain of fallbacks** (owner's answer to Q1: `--root` exists, and the fallbacks
  of the current design stay). Each mode reads only its own key, and the first source that gives a value wins:

  | Order | System mode (elevated) | User mode |
  | --- | --- | --- |
  | 1 | `--root DIR`, which must be admin-only (else exit code 2) | `--root DIR` |
  | 2 | the root of the installed system gupkg that runs (today's `installed_system_root`) | `[roots].user` of `--settings PATH` |
  | 3 | `[roots].system` of `$App\settings.toml` | `[roots].user` of `$App\settings.toml` |
  | 4 | built-in `C:\opt` | built-in `%USERPROFILE%\opt` |

  - **One fallback of the current design is dropped.** The system root's `settings.toml` no longer moves the user root,
    because reading it from a user run is exactly the cross-mode read that requirement 2 forbids.
  - **`[roots]` is never read from a root's own `.gupkg\settings.toml`.** A root cannot name itself, so such a key there is
    a settings error.
  - The chosen root and where it came from (`user root C:\Users\x\opt (built-in default)`) are printed in the Resolve phase
    and logged, as the mode reason is today.
- **An elevated process runs only an admin-only gupkg tree** (`enforce_bundled_runtime`, unchanged). A user-installed,
  bundled-in-Downloads or source gupkg started elevated refuses to start, with exit code 2.
- **The user mode never reads anything under the system root** except the gupkg program it runs and the shipped
  `$App\settings.toml` that belongs to that program.
  - **Code may be shared, data never.** A standard user may run the system gupkg's program in user mode. It is trusted
    code. That run reads and writes only the user root.
- **Nothing names the other mode.** `Scope` has no field that could hold the other mode's root, settings or cache. A
  mode-qualified selector or a path into the other mode's root is "not in this scope" (exit code 2). That error is a
  plain containment check on one root, not a list of refusals.

### 4.2 Settings: one root layer per run (schema 4)

- **Layers:**
  1. built-in defaults;
  2. `$App\settings.toml`;
  3. `<root>\.gupkg\settings.toml`;
  4. `--settings PATH` (user mode only; the system mode refuses the option).
- **`[roots]` stays,** with only the keys `system` and `user`, for the fallback chain of §4.1. It is read from the app
  layer and from `--settings`, never from a root layer.
- **Removed:** `default_mode`, `[roots].portable`, `LAYER_SYSTEM`, `LAYER_USER`, `LAYER_PORTABLE`, `_check_roots`, and the
  "read the system layer first to find the user layer" order.
- **`[integration]` becomes one table:** `bin`, `start_menu`, and `integrate = "full" | "none"`. Its defaults depend on the
  mode. `integrate = "none"` is a property of a scope: shims go to `<root>\bin`, nothing else is written, and
  `gupkg shell` prints the `PATH` line. It replaces `[integration.portable]`.
- **`settings init|set` and `registry add|remove`** write layer 3 of the run's root. They no longer branch on the mode or
  refuse anything.
- **`[manager] checksum_missing`** is read from the run's own layers. `manager_checksum_policy(elevated=...)` and the "judge
  as the child would" logic go.
- **No migration:** a schema 3 file is a settings error that names the key to change. The owner's roots have no
  `settings.toml` today (§2), so this changes nothing on that machine.

### 4.3 Catalog: one root

- **`Catalog(scope)`** scans one root and joins one registry cache, `<root>\.gupkg\registry`.
- **Removed:**
  - `Root.mode`, `Root.adhoc`, `Root.single`, `_trusted_roots`, `_root_for_mode`, `_require_system_path` and `_root_of`'s
    ad-hoc branch;
  - `AmbiguousSelector`: a name is unique inside one root;
  - the registry fallback across modes, and the shadowing warnings.
- **Selectors:** `name`, `name@version`, `registry/name`, or a path. A path must lie inside the run's root, or be a
  distribution folder outside it that is copied in (design §21.4, unchanged).
- **`PackageView.mode` goes.** The mode belongs to the run. `list --json` puts `mode` and `root` once, at document level.
- **`state.toml`** keeps only the last registry sync. `[[adhoc]]`, `register_adhoc` and `unregister_adhoc` go.

### 4.4 No elevation inside gupkg

- **Removed:**
  - `relaunch.py` and `System.relaunch_elevated` (`windows.py`, `fake.py`, `port.py`);
  - `ElevatedRun`, `--relaunched`, `--batch-confirmed` and `--stop-file`;
  - `_groups`, `_child_argv`, `_child_run_end`, `_missing_run_end` and the `mixed-modes` handling;
  - the "confirm the elevation" question;
  - `child-no-result`, `child_raw_exit_code` and the `relaunched` key of the JSON documents.
- **The machine-interface version becomes `"api": 2`.**
- **To act in system mode**, run gupkg or the manager elevated. To keep this one click, the gupkg package installed in
  system mode adds a Start Menu shortcut "gupkg manager (administrator)" whose link carries the "run as administrator"
  flag. Windows asks for UAC, and gupkg contains no elevation code. This needs a `run_as_admin = true` field in
  `[[shortcut]]` (small, and it lives in `system/shell_link`).
- **What stays, because it is threat model T1 and not mode-mixing:**
  - the launcher ACL check (design §21.3);
  - `enforce_bundled_runtime`;
  - the admin-only checks on code and caches *inside* the system root: hooks, locks, the work folder, the HTTP cache and
    trusted `git`. These guard against broken inheritance inside the root (D30, spike S1).
  - The engine reaches them through one call, `scope.require_trusted(path)`, which does nothing in user mode.
    The scattered `if elevated:` / `is_elevated_run(system)` branches go.
- **What stays for its own reasons:** `--events-file`, so the UI can follow a run, and cancelling through the stop marker
  (D37). Their justification ("an elevated child cannot share pipes") is removed from the docs.
- **The unsafe-character rules** stay where a `.cmd` launcher receives the argument, and nowhere else.

### 4.5 The manager has one scope per run

- **Elevated:** system packages, the system registry, system settings. **Normal:** that user's. The title bar shows
  `system · C:\opt` or `user · C:\Users\<name>\opt`.
- **Removed:**
  - the elevated-shell refusal in `startup.py` and in the `tui`/`manager` forwarders;
  - `can_elevate`, `NEEDS_SYSTEM_GUPKG`, the two-group logic and the labels of `steps.py`. One operation is one CLI call;
  - `UiSettings.root_of(mode)`, `default_mode` and `checksum_policy(mode)`;
  - the Mode column of the browser.
- **Scheduled runs:** "Run with highest privileges" now means "manage in system mode". It is no longer refused. D22's
  "a normal shell only reports system packages" goes.
- **From issue 034, required in the same pass:**
  - one typed reader serves both the CLI's JSON and the UI, and replaces the re-parsing in `gupkg_ui/plans.py`;
  - anything unreadable or failed shows on screen;
  - the manager reads in-process, which amends D44: reads in-process, operations through the CLI;
  - tests run against recorded real CLI documents, including `list --available`.

### 4.6 Portable is a kind of package, not a mode

- **A package is portable exactly when its `pkg.toml` has a `[portable]` table.** Portable means its state lives in
  `<package>\data` and is handed over between versions (persist and markers, `engine/portable.py` `hand_over`/`hand_back`).
  A package without the table is non-portable, and nothing about it changes with portability.
- **A package declares which modes it may be installed in** (D55). This is a kept and required capability, independent of
  portability:
  - a system-level driver, a service or anything that writes machine-wide state is **system only**;
  - a tool that only makes sense per profile (or a portable package, below) is **user only**;
  - most packages allow both.
  The registry declares no single-mode package today. The feature still stays.
- **Enforcement of the mode list:**
  - It is checked in the planner, before any download or file change, for every operation that installs or activates a
    version (`install`, `update`, `switch`, repair, `check-updates` with download).
  - The refusal says only what the run can know: "this package installs only for a single user; run gupkg without
    administrator rights", or "…only system-wide; run gupkg as administrator". It never looks at the other mode.
  - The planner checks the version being activated, so a new version that narrows its modes is refused on a scope
    where it no longer applies. The installed version keeps working, and the package is reported `outdated`, with the
    refusal as the reason it cannot update.
- **How it is written** (owner's answer to Q5): the `modes` key stays, so the 130 non-portable definitions need no edit.
  Its meaning becomes:
  - `["system", "user"]` (the default), `["system"]` or `["user"]`: the modes the package may be installed in;
  - `["portable"]`: the package is portable, and that implies user only;
  - a list that mixes `portable` with a mode is **invalid** ("a package is portable or not: publish two packages").
- **A portable package installs only in user mode.** The mode rule above enforces it, with no separate check.
  Shared writable state next to code that an elevated gupkg runs is the reason design §6.3 gave already. In user mode
  it installs into the user root like any other package, with normal user integration.
- **Removed:**
  - `[user]` and `[system]` step tables: their steps move into the package's plain `[[prepare]]`. copyq's user-only
    step is handled below;
  - `[portable].prepare`, `[portable].args`, `[portable].env` and `$Data`: not used anywhere in the registry
    (owner's answer to Q6);
  - `[app] prepared_for`, `mode_signature`, `_guard_the_app` and the "rebuild for another mode" question: an `App` belongs
    to one definition;
  - the portable root, `[roots].portable`, D42 (in-place shape, configured root, the question) and `_check_portable_root`;
  - `--mode portable`, `MODE_PORTABLE` and `ModeContext.in_place`.
- **Registry: every package is converted** (owner's answers to Q2 and Q2a):
  - each of the 28 tri-mode packages becomes two packages:
    - the base name, non-portable, with `modes = ["system", "user"]` and its `[user]`/`[system]` steps as plain
      `[[prepare]]`;
    - `<name>-portable`, with `modes = ["portable"]` and the `[portable]` table;
  - the 6 portable-only packages (sqlitebrowser, SumatraPDF-portable, tixati) and the 130 non-portable packages change
    only where a rule requires it (removed keys).
  This work happens in the registry repository, with a script in its `_tools`. It is **not** gupkg core code.
- **copyq** (owner's answer to Q2b; investigated 2026-10-26).
  - **How CopyQ chooses its storage.** This is `portableFolder()` in upstream `src/platform/win/winplatform.cpp`, read on
    `master`. CopyQ runs portable when the folder of `copyq.exe` is writable **and** holds no `unins000.exe`. It then keeps
    settings, clipboard items and state in `<App>\config` (INI format, never the Windows registry) and logs in
    `<App>\logs`, unless `COPYQ_LOG_DIR` is set. Otherwise it uses `%APPDATA%\copyq`. No flag or variable selects the
    settings folder.
  - **This matches the registry's comment** (commits `cf0ada6`, `ee5e8a4`). The `unins000.exe` step exists because a user
    root is writable: without it, a non-portable user install would silently go portable and lose its settings at every
    update, because `config` would live inside a version's `App`.
  - **`copyq`, non-portable,** `modes = ["system", "user"]`, with
    `[[prepare]] ["create", "unins000.exe"]` and no `[portable]` table. In user mode the empty `unins000.exe` keeps
    CopyQ on `%APPDATA%\copyq`. In system mode it changes nothing, because the `App` folder is admin-only and CopyQ
    already uses `%APPDATA%`. It also makes that behaviour independent of the folder's ACL, so it runs in both modes
    without a mode-specific step.
  - **`copyq-portable`,** `modes = ["portable"]`, with no `unins000.exe` step and `[portable] persist = ["config"]`.
    `config` holds the settings, the items and the state, and survives updates in `<package>\data`. `logs` is not
    persisted: it starts empty in each new version (only diagnostics are lost).
  - **Check in the acceptance run** (§9 item 6, user mode, test root):
    - `copyq` writes `%APPDATA%\copyq` and nothing in `App`;
    - `copyq-portable` writes `App\config`, which resolves to `data\config`, and nothing in `%APPDATA%\copyq`;
    - after an update, the portable settings and items are still there.
  - **Verification:** `PORTABLE-UNVERIFIED.md` gets a row for `copyq-portable`. Its evidence is the upstream source quoted
    above. Its confidence is high for the selection rule; the persist of `config` through a junction is untested.

### 4.7 Install record and receipts

- **The install record keeps its schema.** Its `mode` key is written as the mode. A record whose mode differs from the run's
  is a `broken` reason ("this package was moved between scopes"). It is no longer a mode decision. A record that
  says `portable` is reported the same way and repaired by reinstalling (no reader of old values).
- **Receipts** are read only from `App.info`. A version that has only an old `.gupkg\receipts\<v>.toml` shows "no receipt"
  until it is reinstalled or repaired (§3.3).

### 4.8 Distribution folders and `--in-place`

- **Unchanged:** a distribution folder outside the run's root is copied into it (design §21.4, D17 with "the mode's root"
  read as "the run's root"). The system mode copies only from an administrator shell, as today.
- **`--in-place` and ad-hoc roots go** (owner's answer to Q1). A folder that should stay where it is becomes the root of
  its own user scope (`gupkg install <folder> --root <folder above name>`). Its launchers already treat
  `%~dp0..\..` as the root, so a bundled install in place needs no new mechanism. The user root's manager does not list
  such a folder: it is another scope.

### 4.9 Launchers: bundled, then the root's setting, then `PATH`

This is the owner's answer to Q4. It amends D16 (which allowed no `PATH`) and D32 (which put the installed gupkg before
the bundled one). A launcher lives at `<root>\<name>\<version>\`, so its root is `%~dp0..\..`.

1. **The bundled gupkg,** `%~dp0gupkg\gupkg.cmd`, when the version directory carries one.
2. **The gupkg named by the root's setting.** `<root>\.gupkg\gupkg-location` (name proposed) is a one-line text file
   holding the folder that contains `gupkg.cmd`. A relative path is resolved against the root. The launcher reads it with
   `set /p`, and nothing in it is executed. It is data, not a script calling a script.
   - Installing the `gupkg` package into a root writes this file, pointing at `$PackageRoot\current\App`. That replaces
     `[root_forwarder]`. Uninstalling gupkg removes the file, but only if it still holds that value.
   - A person may edit it, for example to let a user root use the system gupkg's program.
     - *Code may be shared, data never* (§4.1): the program it names still runs as a user-mode run on that root.
   - A missing file, an empty file, or a named folder without `gupkg.cmd` means "no setting". The launcher goes on to
     step 3 and prints which file it skipped.
3. **`gupkg` on `PATH`.**
   - **Not elevated:** the first `gupkg.cmd` or `gupkg.exe` that the process `PATH` finds.
   - **Elevated** (owner's answer to Q7: (b)):
     - **Which entries:** the process `PATH` is scanned in order, and an entry is used only if its folder passes the
       admin-only check. A user-writable entry is skipped and named in the output.
     - **No registry read:** the launcher never reads the machine `PATH` from the registry, so D18 stays whole.
     - **What must be admin-only:** the whole chain of what will run: the shim, its `.config.toml`, the target it names,
       and that target's gupkg tree, recursively, with the existing ACL check (D30). Fails closed.
     - **Accepted consequence:** an entry from an administrator's own user `PATH` is used when its folder happens to be
       admin-only. Only an administrator can change such a folder, so threat model T1 is unaffected.
4. **Else** exit 9009 with the bootstrap hint.

The elevated launcher's ACL check (design §21.3) covers the root, the package and version directories, `.gupkg\` with the
`gupkg-location` file, and the tree of whichever gupkg step 1, 2 or 3 selects.

- **Removed:** the forwarder `.cmd` files, the `[root_forwarder]` manifest table and its effect, and
  `--no-system-gupkg=true`. With the bundled copy first, the option has nothing left to skip.
- **The exit code 6 fallback reverses.** When the selected gupkg is too old for the package (exit code 6), the launcher
  tries the next step in the order above.
- **`cleanup --bundled`** removes bundled copies only where step 2 or 3 would find a gupkg. Otherwise the packages'
  launchers would be left with nothing to run.

### 4.10 Migration code

Everything in the table of §3.3 is deleted. The tests and fixtures that only served it are deleted. The four test modules
that borrowed `registry_017` get format 2 fixtures, or lose the cases that existed only to compare 0.17 with 1.0. The
"0.18 → 1.0" text leaves the bootstrap and the docs.

**The owner's installed packages are not touched** (owner's answer to Q3). No step of this issue converts, repairs,
reinstalls or deletes anything in `C:\opt` or `C:\Users\guraltsev\opt`, and no test or acceptance run uses those roots
(§9). This is known and accepted:

- the 33 0.17-style packages (16 user, 17 system) lose their only converter;
- the 14 tri-mode packages and the 6 with `[user]`/`[system]` tables in `C:\opt` show as `invalid` under the new gupkg,
  until the owner deals with them.

## 5. Functionality: what is kept, what moves, what is removed

| Capability today | After |
| --- | --- |
| Install a system package from a normal shell with one UAC prompt | Kept as a way of starting: the "gupkg manager (administrator)" shortcut, or an admin shell. The CLI no longer relaunches. |
| One manager screen with system and user packages | Two runs: an elevated manager and a normal manager. |
| `system:firefox` and `user:firefox` from one shell | One per mode, from that mode's shell. |
| A package installable both portable and non-portable | Two packages (`x` and `x-portable`). |
| A package restricted to system only (a driver, a service) or user only | Kept (D55). It is checked by the planner against the run's mode. |
| Portable root with `integrate = "none"` (USB stick) | A user scope rooted on the stick (`--root E:\opt`) with `[integration] integrate = "none"` in its own settings. |
| `--in-place` / ad-hoc roots listed by the user's manager | The folder is its own scope (`--root`). The user root's manager does not list it. |
| Configurable roots (`[roots]`, the installed system gupkg's root) | Kept as the fallback chain of §4.1, minus the system layer moving the user root. |
| Launchers reach an installed gupkg through the root forwarder | Bundled first, then the root's `gupkg-location` setting, then `PATH` (machine `PATH` only, and admin-only, when elevated) (§4.9). |
| Per-mode `[user]`/`[system]` steps | Plain `[[prepare]]`. The two tables are identical in 5 of the 6 registry packages that use them. copyq's user-only `unins000.exe` step runs in both modes in the non-portable `copyq`, where it is harmless in system mode; `copyq-portable` omits it (§4.6). |
| `[portable] args`/`env`, `$Data`, `[portable].prepare` | Removed. Unused (owner's answer to Q6). |
| User root that falls back to the system registry cache | Each mode syncs its own registry. The status line says when it has never been synced. |
| Shadowing warnings | Removed. Windows merges `PATH`. That is not gupkg's business (issue 034). |
| `gupkg migrate`, adoption of 0.17 installs, old receipt path, 0.17 option hints | Removed. |

## 6. Decisions

### 6.1 Decisions (D47–D55 are proposed; the owner confirms them in phase 0. The answers to Q1–Q6 are recorded in §6.2)

| # | Decision | Replaces |
| --- | --- | --- |
| D47 | A run belongs to one scope (mode, root), fixed at process start. The mode comes from the token alone. | D4, design §6.2, §5.3 rule 5 |
| D48 | The root of a run comes from the per-mode fallback chain of §4.1: `--root`, then (system) the installed system gupkg's root or (user) `--settings`, then `$App\settings.toml`, then the built-in default. A root's own settings never name a root. An elevated run needs an admin-only root and an admin-only gupkg tree. | design §11 layers 3–5 as sources of roots |
| D49 | gupkg never elevates itself. The system mode is reached by starting gupkg elevated, helped by a run-as-administrator shortcut. | design §9.6 rule 1 (relaunch part), §10.4 "The elevated child", D24 (relayed part), D31 (now structural) |
| D50 | Each mode has its own settings, registry cache, state and logs, and never reads the other's. | design §6.1 last row, §9.4, §11 |
| D51 | Portable is a package kind (`[portable]` present), installed in user mode only. A definition is never both. | D7 (kept for persist), D42, design §6.3 "where a portable package goes" |
| D52 | Launchers try the bundled gupkg, then the one named in `<root>\.gupkg\gupkg-location` (data, not a script), then `PATH`. When elevated, only `PATH` entries whose folder is admin-only are used (no registry read), and everything selected must pass the admin-only check. No root forwarder. | D16, D32, D20 (forwarder part), design §21.3 |
| D53 | One manager scope per run. Elevated is allowed. Reads happen in-process through a shared typed reader. Operations go through the CLI. | D44 (amended), design §13.2 step 4, §15 "Scheduled runs" |
| D54 | No migration code in the repository, core or not. | D33, D45 (`migrate` forwarder), design §18 |
| D55 | A package declares the modes it may be installed in: system only (drivers, services, machine-wide state), user only, or both (the default). A portable package is user only. The planner refuses a version on a mode it does not allow, before any work, and the message never names the other mode's locations. | the mode part of `modes` in design §6.2 |

### 6.2 Owner's answers (2026-10-26)

All questions are answered. Nothing is left open for phase 0 except confirming D47–D55 in the design note.

| # | Question | Answer |
| --- | --- | --- |
| Q1 | `--root DIR`, and how roots are found | **Yes**, and the fallbacks of the current design stay. See §4.1. Only the system layer moving the user root is dropped (requirement 2). |
| Q2 | The registry packages | **Convert all registry packages.** All 164 `pkg.toml` files are brought to the new rules in the registry repository. Read here as: each of the 28 tri-mode packages becomes **two** packages, the base name non-portable and `<name>-portable`, so no functionality is lost. The 130 non-portable and 6 portable-only packages change only where a rule requires it. |
| Q3 | Existing installs on the owner's machine | **Do not touch them** (§4.10). |
| Q2a | Does Q2 mean two packages for each of the 28? | **Yes.** |
| Q2b | copyq's user-only step | **Investigate, then make a portable and a non-portable copyq.** See §4.6, "copyq". |
| Q4 | How launchers find gupkg | **Bundled, then the root's config string in `<root>\.gupkg`, then `PATH`.** When elevated, only what is not user-writable. See §4.9. |
| Q7 | How an elevated launcher limits `PATH` | **(b):** scan the process `PATH` and use only admin-only folders. No registry read; D18 stays whole. |
| Q5 | Spelling of the mode restriction and portability | **(a): keep `modes`** with the meaning of §4.6. No edit to non-portable definitions. |
| Q6 | Unused portable features (`args`, `env`, `[portable].prepare`, `$Data`) | **Remove them.** |
| Q8 | Names (2026-10-28) | The run's object is **`Scope`** (`mode`, `root`, …); the system/user field is **`mode`**, and the `pkg.toml` key stays `modes`. Today's `system.port.Scope` enum becomes `Mode` (§4.1). "Installation" and "side" are not used for this. |
| Q9 | How a `[[shortcut]]` says which modes it applies to (2026-10-28) | **A `modes` list**, same form as the package's, defaulting to the package's modes and a subset of them. See job 09, T08. |

## 7. Expected size

These are estimates, to be measured after each phase. The point is the direction.

| Area | Now (lines) | Expected |
| --- | --- | --- |
| `modes.py` + `model/mode.py` | 892 | ~250: one `Scope` builder and the distribution copy |
| `relaunch.py` + relaunch in `system/*` + `cli.py` child code | ~600 | 0 |
| `catalog/catalog.py` | 1,012 | ~750 |
| `settingsload.py` + `model/settings.py` | 606 | ~400 |
| `engine/lifecycle.py`, `planner.py`, `portable.py`, `stateio.py` | 1,854 | ~1,550 |
| `model/manifest.py` (mode steps, portable variants, root forwarder) | 1,754 | ~1,550 |
| `gupkg_ui/steps.py`, `settings.py`, `startup.py`, `plans.py`, `flows.py` | 997 | ~600 |
| `gupkg_migrate/` + launcher + core legacy bits | ~1,700 | 0 |
| **Source total** | | **about −3,000 to −3,500 lines** of the 37,700 |
| Tests: `test_cli_relaunch`, `test_cli_elevation`, `test_mode_*`, `mode_support`, `test_engine_adhoc`, `test_migrate_*`, `test_core_without_migration_tool`, and parts of `test_settings_layers`, `test_catalog_security`, `test_engine_modes`, `test_ui_flows` | ~6,000 | about −4,000. Fixtures: −476 KB. |

## 8. Plan

Each phase leaves `main` green (`scripts/test.cmd`) and is a commit of its own, made only when the owner asks (AGENTS rule 9).

0. **Decisions and design note.**
   - The owner confirms D47–D55. All questions are answered (§6.2).
   - Write the new §6 ("Scopes and package kinds") in `rehaul_design.md`, update §§9.4, 9.6, 10.4, 11, 12, 13.2, 15,
     19 and 21, delete §18, and mark D4, D16, D20, D22, D24, D31, D32, D33, D42, D44 and D45 as superseded by D47–D54.
   - Update `_docs/elevated-runtime-design.md`.
1. **Remove migration code** (§4.10). It is mechanical and independent, so it goes first and shrinks every later diff.
   It touches no installed package.
2. **`Scope` and settings schema 4** (§4.1, §4.2). `Scope` replaces `ModeContext`, `select_mode`,
   `resolve_mode`'s mode logic and `settings_loader`'s mode arguments. Every caller takes the run's scope.
3. **Catalog over one root** (§4.3), and the end of ad-hoc roots.
4. **Remove the relaunch** (§4.4). `"api": 2`.
5. **Manager: one scope, in-process reads, shared reader, visible failures** (§4.5, the issue 034 defects).
6. **Portable as a package kind** (§4.6, §4.7): manifest, engine, receipts. The conversion of all registry packages
   (Q2) lands in step with this phase, because a registry `pkg.toml` that mixes `portable` with a mode becomes invalid.
   - The conversion is a script in the registry repository's `_tools`, not gupkg code.
   - Its output passes `gupkg_registry validate` and `_tests`.
   - Each new `<name>-portable` package gets its own `_check_updates` and fingerprints.
7. **Launchers** (§4.9): bundled → `gupkg-location` → `PATH`, `gupkg-location` written by the gupkg install, and
   `cleanup --bundled`.
8. **Docs pass**: `README.md`, `_docs/*`, `--help`, the TUI help text and key hints. Then a consistency sweep (AGENTS
   rule 13).

## 9. Acceptance

Run each item for real, elevated and normal, and report the output (AGENTS rules 3, 8, 12).

**Where these runs happen:** the owner's roots (`C:\opt`, `C:\Users\guraltsev\opt`), their `bin` folders, Start Menu and
`PATH` are never used (Q3). Real runs use:

- separate test roots (for example `D:\gupkg-test\sys` made admin-only, and `D:\gupkg-test\user`), with `--root`, test
  `bin` and Start Menu folders set in their settings;
- or `--sandbox`;
- or a disposable VM for the bootstrap and the machine-`PATH` items.

1. **The system mode does not consider user-writable locations.**
   - Prepare a user root with a settings file that is not valid TOML, a poisoned `state.toml`, a package with a hook that
     writes a marker file, and a registry cache with a different revision.
   - An elevated `list`, `check`, `install`, `update`, `cleanup`, `registry sync` and manager run succeed, never mention
     any of them, and the marker is never written.
   - The same with the roles swapped: a normal run never reads `C:\opt\.gupkg\*`.
2. **Structure.**
   - `Scope` has no field for another root.
   - `grep -rn "is_elevated_run\|elevated" src/gupkg` finds only startup and `Scope`.
   - `grep -rn "portable" src/gupkg` finds only the package kind and `engine/portable.py`.
   - `grep -rni "migrat\|legacy\|0\.17" src` finds nothing that is code.
3. **A fresh install of gupkg, system and user**, by bootstrap. In system mode, the shortcut "gupkg manager
   (administrator)" opens the system manager after UAC.
4. **gupkg updates itself** in each mode, with a failure part-way that leaves a working `current` and says what state the
   package is in.
5. **First manager run with an empty registry** in each mode: sync, `a`, install one package. Each result shows on screen.
   The title shows the mode and the root.
6. **Update, check, repair and uninstall** in each mode. A portable package installs, updates (persisted files survive)
   and uninstalls in user mode. The system mode refuses it before any work, and the message says why.
7. **Mode restrictions (D55).**
   - A `["system"]` test package installs elevated and is refused in user mode.
   - A `["user"]` package installs in user mode and is refused elevated.
   - Each refusal comes before any download or file change. A test checks that the package root and the bin folder are
     unchanged.
   - A new version that narrows its modes leaves the installed version working, and `update` reports why it does not
     apply.
8. **Root and launcher lookup.**
   - **The root fallback chain:** each row of the §4.1 table wins over the rows below it, in each mode, and the Resolve
     line names the source.
   - **A `[roots]` key in a root's own `settings.toml`** is an error.
   - **The launcher order:** bundled, then `gupkg-location`, then `PATH`. A missing, empty or wrong `gupkg-location`
     falls through with a message.
   - **Elevated, the launcher refuses:**
     - a gupkg found only through a `PATH` entry whose folder is user-writable (the entry is skipped and named);
     - a `gupkg-location` that names a user-writable tree;
     - a shim whose config or target is user-writable.
   - **Exit code 6** moves on to the next step.
9. **Failures:** offline; a locked file; a missing `current`; an elevated start of a user-installed gupkg (refused at
   startup with exit code 2). Each failure reports what happened and how to recover.
10. **Test counts:** the suite passes, and the report gives the counts before and after, plus the source and test line
   counts before and after.

## 10. Rules for this work

From `AGENTS.md`, and the reason this issue exists:

1. Phase 0 comes first. No code before the owner has confirmed D47–D55 and the design note is written.
2. Reproduce before changing behaviour. Each removed capability gets a test that shows the new refusal or the replacement.
   A regression test must fail without its change.
3. gupkg changes machine state. Never touch `C:\opt` or the user root without being asked. After any failed step, say what
   state they are in and the one command that repairs them.
4. Do not commit, release or bump versions unless asked. State what is uncommitted when stopping.

## 11. Inconsistencies found while writing this (AGENTS rule 13)

Fix only those this issue covers. Report the rest.

1. **The installed `gupkg` shim fails with any argument**, run from PowerShell or bash:
   `gupkg --version` prints `'C:\opt\gupkg\current\App\gupkg.cmd" "--version' is not recognized as an internal or external
   command`. `C:\opt\gupkg\current\App\gupkg.cmd --version` prints `gupkg 1.0.8`. The shim config is
   `target = "C:\\opt\\gupkg\\current\\App\\gupkg.cmd"`, `forward_arguments = true`.
   - *Hypothesis, not reproduced in a test:* the shim starts `cmd /c "<target>" "<args>"`, and `cmd` strips the outer
     quotes.
   - This is a separate bug. Give it its own issue.
2. **`C:\opt\.gupkg` has no root forwarder** although the installed gupkg 1.0.8 declares `[root_forwarder]`
   (`C:\opt\gupkg\1.0.8\pkg.toml:31`). Moot after §4.9, but it means the system root's launchers already fall back to
   bundled copies today.
3. **Issue 034's "State of the working tree" is stale** (§2). Its "Self-update can delete `current`" item has a committed
   fix (`3270152`) whose test was recorded as proving nothing. That has not been re-checked.
4. **Design §6.1** says the default portable root is "a scanned location, not a target". The catalog scans only the
   configured `[roots].portable`, which defaults to that same folder, so the two statements say the same thing in two ways.
   Moot after §4.6.
5. **The registry's `PORTABLE-UNVERIFIED.md`** still describes `[nonportable]` tables, which core rejects since the
   `[user]`/`[system]` change.
