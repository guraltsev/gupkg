# Issue 034: System and user installations must be completely independent

> **Status (2026-10-29):** Superseded by issue 035 (one scope per run), implemented by job 09. Where each required item landed:
> two independent catalogs, per-side registry, settings, state and logs, and no cross-side warnings: T04 (settings schema 4 and the root fallback chain), T05 (one `Scope`; the mode comes from the token) and T06 (the catalog over one root);
> the elevated run names no user location (structural, `Scope.require_trusted`): T05, T06 and T07 (the elevated relaunch is removed);
> the manager has one scope per run, no elevation prompt and no refusal to start when elevated: T07 and T09;
> `--mode` and `default_mode` stop choosing the side: T05;
> the shared typed reader and visible failures instead of silent ones: T10 (`gupkg.report.documents`; `read_results` is not yet used by the UI session, see the T12 log);
> the root forwarder is replaced by `<root>\.gupkg\gupkg-location`: T11;
> portable became a package kind: T02 and T03; open questions of this issue are answered in issue 035 section 6.
> Not part of issue 035: the self-update junction defect was fixed before job 09 (commit 3270152); the speed item (cost of each `gupkg` call) was not worked on.

Date: 2026-10-25
Priority: High
Design touched: `_docs/rehaul_design.md` §6 (install modes), §9.4 (elevated runs), §9.6 (threat model), §13.2 (batches), §15 (UI programs), §21.3 (launchers and the root forwarder); decisions D4, D16, D20, D30, D31, D32, D44

> **Status:** Open. This is a design correction, not a bug fix. Read "Rules for this work" before touching code.

## Objective

The owner's requirement, stated in their words and then in ours:

- **System-wide and per-user installations are two independent systems that know nothing about each other.**
  - A system-wide package A and a package B that user U installed in a folder of their choosing are unrelated.
  - B has no meaning at the system level. User V, on the same machine, sees the system packages and V's own packages, never U's.
- **An elevated run must not even *consider* anything a standard user can write.** Not "look, then refuse". It must have no way to name such a location.
- **An elevated manager manages system packages. A normal manager manages that user's packages.** They are separate runs of the program.
  - The manager never launches an elevated child. There is no administrator prompt inside it.
  - The manager never refuses to start because the shell is elevated: being elevated is what makes it the system manager.
- Each side has its own registry cache, settings, state and logs. Nothing is merged across them.

## Why this is an issue: the current design is the opposite

Everything below was observed on 2026-10-25 on the owner's machine (gupkg 1.0.8; roots `C:\opt` system with 83 packages, `C:\Users\guraltsev\opt` user with 15) or read in the code. Items marked *(inferred)* were not verified.

1. **One catalog with an elevated filter on top.**
   - `Catalog.from_settings` ([catalog.py:344-368](../src/gupkg/catalog/catalog.py)) always lists every root (system, user, portable). In an elevated run it then skips the others one at a time (`if is_elevated and mode != "system": continue`).
   - `_trusted_roots` ([catalog.py:702-722](../src/gupkg/catalog/catalog.py)) still examines each root and adds `"Skipped <path> in an elevated run"` for the rejected ones.
   - An elevated run reads the system `state.toml` for "ad-hoc roots" (arbitrary recorded locations), then tests each for trust.
   - About 115 `elevated` checks across 49 files implement this. Each is a place someone must remember to filter; nothing structural prevents a miss.
2. **The manager is a standard-user program that shows both worlds and calls an elevated child for one of them.**
   - `build_environment` refuses an elevated shell ([startup.py:117-119](../src/gupkg_ui/startup.py)).
   - `steps.py` splits every operation into one normal call plus one elevated call for all system targets.
   - `gupkg_ui/settings.py` judges system targets "as an elevated run would see it", because the normal process decides for the elevated child.
3. **`gupkg list --json` merges both roots** (98 rows: 83 system, 15 user). The browser shows one table with a Mode column, one "n outdated · n broken" count and one registry status line.
4. **Cross-root awareness exists on purpose.** gupkg warns `shadowing: 'restic' exists in both the system and the user root`. Both `catalog.py:421-447` and `modes._shadow_warnings` compare roots. The user settings layer also reads the system root's settings (§6.1 table, "Settings layers read").
5. **The mode is chosen by whether the terminal happens to be elevated** (`default_mode = auto`, D4), instead of by which installation the run is.

## Symptoms the owner hit (all follow from the above)

- Registry shows `-` for every package. Cause, verified: the manager runs unelevated, so `auto` picks `user`, and `registry sync` only syncs the user root. `C:\opt\.gupkg` has no `registry` folder, so none of the 83 system packages follows its registry entry (69 of them have a same-name entry in the synced registry). The 3 user packages that have one (`restic`, `resticprofile`, `SumatraPDF-portable`) follow correctly.
- The status line says "registry main @ af565e2 (synced 1m ago)" while the root that holds most packages was never synced. One registry state is shown for a screen that shows two roots.
- Pressing `a` (show registry-only packages) adds nothing. Cause, verified: `read_packages` ([plans.py](../src/gupkg_ui/plans.py)) drops every row without a `mode`, and the CLI gives `mode: null` for packages that are not installed. This is a separate defect that is a symptom of the same cause: the UI re-parses the CLI's JSON with its own rules.
- `s` and `a` were not visible in the key hints in the build the owner ran (1.0.5). That build was older than the source: the installed gupkg lagged HEAD.

## Required behaviour

1. **Two independent catalogs.** An elevated process builds a catalog from the system root only. A normal process builds one from that user's root only. No code path names the other side's locations, and there is no filter to forget.
2. **The elevated run never touches user-writable anything**: no user settings layer, no user root, no ad-hoc root outside the admin-only system data directory, no portable root, no `%USERPROFILE%`-relative default. Prefer making these *unrepresentable* (the system catalog has no field to hold them) over checking them.
3. **The manager has one scope per run.** Elevated → system packages, system registry, system settings. Normal → that user's. The title bar or status line states which. No elevation prompt inside the manager, and no refusal to start when elevated.
4. **Registry, settings, state and logs are per side and never merged.** Sync, status, `a`, the Registry column and install all act on the side the run is.
5. **No cross-side warnings.** The shadowing warnings go away with the cross-side comparison. Windows still merges PATH; that is not gupkg's business.
6. **`--mode` and `default_mode = auto` stop being the way a side is chosen.** The side is the run. Decide in the design what remains of `--mode` (see open questions).

## Related defects to fix in the same pass (not optional)

- **UI parsing duplicated.** The manager parses the CLI's JSON with its own rules (`gupkg_ui/plans.py`), which is how `mode: null` was lost. One shared, typed reader must serve the CLI output and the UI. Test it against **recorded real CLI documents** for every command variant, including `list --available`. The current test double (`tests/ui_support.py`, `FakeGupkg`) keys responses by the first word of the command, so `list` and `list --available` cannot be told apart, and its documents never had `mode: null`.
- **Silent failure.** `read_packages` drops rows without a message; `ManagerApp.read_registry` swallows `ClientError` and shows nothing. Anything unreadable must show on screen. An operation that fails must say so.
- **Self-update can delete `current` and leave gupkg unlaunchable.** Observed 2026-10-25: updating gupkg 1.0.5 → 1.0.8 failed in `create_junction` with `[Errno 2] ... current\App\gupkg\python\python312.zip`, the rollback did not restore the junction, and `C:\opt\gupkg\current` was gone, so every launcher refused (`GUPKG-ACL-FAIL … current: missing`). *Hypothesis, not proven:* `WindowsLinks.create_junction` removes the old junction first, and the running interpreter reads its standard library through that junction, so the next first-time import (for example the `utf-16-le` codec used to build the reparse buffer) fails. Reproduce it with a real interpreter started through a junction **before** fixing, and make the failure path restore the old `current`. Never leave a package without a working `current`. An uncommitted, unproven edit of `create_junction` exists in the working tree (see below).
- **The root forwarder.** `<root>\.gupkg\gupkg.cmd` and `gupkg-manager.cmd` are a chain of `.cmd` files that call the installed gupkg. The owner's position: the default is the gupkg inside the same root (`<root>\gupkg\current\App\...`), and where a setting is needed it is a setting in a config file, not a script calling a script. Under independent sides the "user root follows the system gupkg" case disappears; re-evaluate whether the forwarder is needed at all.
- **Speed (secondary).**
  - Measured: every `gupkg` call costs 0.4–0.6 s (bare Python start is 0.04 s) because `gupkg.cli` imports the whole program eagerly (`import gupkg.cli` alone takes 0.43 s: `engine.lifecycle`, `authoring`, `pkgcheck`, `hooks` and others).
  - The manager makes 2–3 such calls per screen refresh. With one scope per run it can call the read functions in-process instead of spawning `gupkg.cmd`, as D44 already allows for reads.

## Open questions for the owner (do not guess)

- Portable mode: is it a third independent side, a variant of user mode, or removed? Its rules (`--mode portable`, the portable root, §6.3) were not discussed.
- Should a normal user's manager *show* the system packages (read-only), or nothing from the system side? The owner's wording ("user V then sees what?") suggests V sees the system packages; confirm.
- What stays of `--mode` and `default_mode` once the side is the run? Is `gupkg --mode system` run from a normal shell an error, a UAC relaunch, or gone?
- Is the root forwarder (`.gupkg\gupkg.cmd`) removed or turned into a setting?

## Size

This touches `catalog/` (about 2,100 lines), `modes.py`, `model/mode.py`, `settingsload.py`, `gupkg_ui/` (`steps.py`, `settings.py`, `startup.py`, `plans.py`, `flows.py`) and `gupkg_manager/`. For scale: `src` is about 37,700 lines of Python (about 10,500 of them docstrings), with 40,800 lines of tests and 35,000 lines of fixtures. The task is to **remove** mixed-world code; expect a net reduction.

## Rules for this work

These come from `AGENTS.md` and exist because of how this design went wrong.

1. Read `_docs/rehaul_design.md` §6, §9.4, §9.6, §13.2, §15, §21.3 and the decisions listed above, then write down the new model (a short design note) before editing code. Update or supersede D4, D16, D31 and D44 explicitly.
2. Reproduce each defect first; keep a failing test as proof. A regression test must fail without the fix and pass with it.
3. Test against the **real CLI** in the sandbox for the workflows below. Do not rely on scripted doubles alone.
4. Run the full test suite before saying done, and report the counts.
5. Do not commit, release or bump versions without being asked.

## Acceptance workflows (each run end to end on the installed layout, elevated and normal)

1. Fresh install of gupkg, system and user.
2. gupkg updating itself; a failure in the middle leaves a working `current` and a message saying what state it is in.
3. First manager run with an empty registry: sync, list available (`a`), install one package. The screen shows each result.
4. A manager run elevated shows only system packages; a normal run shows only that user's. Neither shows, names or warns about the other side.
5. Update, check, repair and uninstall of a package in each side.
6. Failures in each: offline, locked file, missing `current`, no administrator rights. Each reports what happened on screen.

## State of the working tree when this was written

Uncommitted and **not reviewed**; review or discard, do not trust:

- `src/gupkg_ui/plans.py`: accepts an empty mode for `available` rows. This fixes `a` and is likely right, but it patches one symptom and is superseded by the shared-reader work above.
- `src/gupkg_manager/app/browser.py`, `src/gupkg_manager/app/app.py`: hint line (`a avail` / `a hide`) and the status line counting available packages.
- `tests/test_ui_screens.py`: a new test for `a` (fails without the plans change; **still times out with it**, a sequencing mistake in the test double's `list` responses, unresolved), and one changed assertion.
- `src/gupkg/system/windows.py`: `create_junction` removes the old junction later. **Unproven.**
- `tests/test_junction_replace_while_running.py`: **passes with and without the edit, so it proves nothing.** Delete or rewrite it.
- `AGENTS.md`: new working rules.
- The `.cmd` files modified before this session are not related to this issue.
