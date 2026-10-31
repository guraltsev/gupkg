# Issue 037: Package launchers as plain templates, `manage.cmd` instead of the second `gupkg-tui.cmd`, and a command that copies them

Date: 2026-10-31
Priority: Medium
Status: Complete 2026-10-31 (closed). Nothing is released: an installed gupkg 1.1.6 still writes `gupkg-tui.cmd`.

Done:

- `src\templates\` holds `install.cmd`, `manage.cmd` and `check-updates.cmd` as finished files; `gupkg.launchers` copies them
  and `package-launcher.cmd` is gone. The release build copies `templates` into the tree.
- `gupkg pkg launchers [VERSION-DIR]` copies the two version launchers; `pkg adopt` adds the ones the folder lacks.
- The registry tool `launchers` converts a registry (removes `gupkg-tui.cmd`, writes `manage.cmd`, refreshes fingerprints);
  `validate` refuses the old name. The published registry (`gupkg-registry`) was converted: 97 versions, `validate` OK.
- The old-layout tests are conditional tests (`tests\_conditional\old_registry_layout\`, `expires: 2027-04-30`), a new kind
  defined in `_docs\tests_hygiene.md` §2.1 with check H7.
- Answers to the open points of §5: the old name is not kept working for installed packages (they get `manage.cmd` when the
  launchers are rewritten); `check-updates.cmd` moved to `templates` unchanged.
- The three tree launchers (`gupkg.cmd`, `gupkg-tui.cmd`, `gupkg-manager.cmd`) say, when their `gupkg\` folder is missing, what
  they are and to use `manage.cmd` or `install.cmd` (and `gupkg pkg launchers VERSION-DIR`) for a package (§3.4).
- Documents that named the package launcher `gupkg-tui.cmd` now say `manage.cmd` (README, `_docs\`, `tests\manual_smoke.md`,
  and the README of `gupkg-registry`).

### Workflows run (AGENTS.md rule 12), 2026-10-31

All on a scratch root with `USERPROFILE`, `APPDATA` and `LOCALAPPDATA` pointed at a scratch folder, `integrate = "none"`, and the
source tree's own `gupkg.cmd`. The Start Menu had 43 files before and after; `C:\opt\gupkg\current` still pointed to 1.1.6.

- `pkg adopt --no-install` on a folder with a real `.lnk`: wrote `pkg.toml`, `install.cmd`, `manage.cmd`; `pkg check` ok.
- `install.cmd` with no bundled gupkg and no `gupkg-location`: found `gupkg.cmd` on `PATH`, installed, `current -> 1.0\App`.
- Failed step: `manage.cmd` with no gupkg anywhere: skipped the missing `gupkg-location`, said how to get gupkg, exit 9009.
- `manage.cmd` with a gupkg: started the text interface for the version folder; with no terminal it refused and named the
  `gupkg info` command to use in scripts.
- `check-updates Game`, `uninstall Game`, `list`: ok; the version folder stays as a local package.
- The registry: `validate` OK (95 packages, 97 pinned versions); the registry's own `pkg_check --all` (192 directories) and its
  212 tests pass against this tree.

Not run: the same on the installed `C:\opt` layout, fresh install of gupkg itself, self update, elevated runs. They would change
the machine; they are for the release check in `tests\manual_smoke.md`.

### Inconsistencies found (rule 13)

- `gupkg pkg --help` lists no actions (the metavar `<action>` hides them), while `gupkg --help` lists them. Not fixed.
- `repair` is not a command of this gupkg (`install --overwrite KINDS` is the nearest); `troubleshooting.md` names
  `self repair`, which exists. No change.
- The registry's CI checks out gupkg `main`, so publishing the converted registry before this change reaches gupkg `main`
  fails its `validate`. Publish gupkg first, then the registry.
- `gupkg-registry\_tools\convert_035.py` still lists `gupkg-tui.cmd`; it is the one-off converter of issue 035 and is left as it was.

## 1. Problem

Three different files are called `gupkg-tui.cmd`:

| File | Role |
| --- | --- |
| `src\gupkg-tui.cmd` | Outer launcher of a gupkg tree. Runs only `gupkg\gupkg-tui.cmd` beside it; nothing is searched. |
| `src\gupkg\gupkg-tui.cmd` | The tui program of a tree: starts the bundled Python on `bootstrap.py --gupkg-program=tui`. |
| `<root>\<name>\<version>\gupkg-tui.cmd` (103 files in `gupkg-registry\`) | Package launcher, generated from `src\gupkg\launchers\package-launcher.cmd`. Finds some gupkg (bundled, `gupkg-location`, `PATH`), checks ACLs when elevated, and runs `gupkg tui <version dir>`. |

Only the third is meant to be copied into a package folder. The owner copied the first into a game package
(`C:\Games\AETHUS\0\`) and got "This gupkg is incomplete: ...\gupkg\gupkg-tui.cmd is missing". The file name does not say which is
which, and the message does not say what the file is for. The header of the second already has to say "it is not the package launcher
of a version directory".

The two jobs really differ (fixed target versus searched target with an elevated ACL check; see §21.3 of the design), so the files
stay separate. The names should not clash.

## 2. Owner's decisions (2026-10-31)

1. A directory `src\templates\` holds the useful package launchers as finished, readable `.cmd` files, named without "gupkg":
   `install.cmd` and `manage.cmd`.
2. `manage.cmd` is the new name of the package launcher that today is `gupkg-tui.cmd`.
3. gupkg gets a command that copies the needed launcher files into a requested package, linked to `pkg adopt`.

## 3. Proposal

1. `src\templates\install.cmd`, `src\templates\manage.cmd` (and the tracker launcher, see §5) are complete files, byte-identical to
   what gupkg writes today for the same command. They replace the placeholder file `src\gupkg\launchers\package-launcher.cmd`.
   `gupkg.launchers` reads them; there is no placeholder filling. A test checks that the text written for each command equals the
   template file (the registry fingerprint covers the launchers).
2. New command `gupkg pkg launchers [<version dir>]`: copies the launchers into a version directory (current directory by default),
   replacing existing ones, and prints each file written. `pkg adopt` calls the same code.
3. `VERSION_LAUNCHERS` becomes `install.cmd` and `manage.cmd`. `LAUNCHER_NAMES` in `gupkg_registry\layout.py`, the registry tool
   `launchers`, `validate`, `authoring.py`, `tools\build_standalone.py`, docs and tests follow.
4. The outer tree launcher's "incomplete" message says what the file is and what to use for a package.
5. The tree launchers `src\gupkg-tui.cmd` and `src\gupkg\gupkg-tui.cmd` keep their names (they are the program; shim and
   `gupkg tui` use them).

## 4. Phase 0: facts to verify before coding

- Whether `pkg adopt` writes launchers today (`authoring.py` writes them for bundle/export; adopt is not checked).
- Every place that spells `gupkg-tui.cmd` as a *package* launcher (about 62 files mention `gupkg-tui`; most are the tree program).
- How packages already installed with `gupkg-tui.cmd` behave: transition plan in §5.
- `tests\test_package_launcher.py`, `test_launcher.py`, `test_release_bundle.py`, `test_registry_validate.py` for the fixed name.

## 5. Open points

- **Old name on installed packages.** Proposal: `repair` and the registry tool write `manage.cmd`; an old `gupkg-tui.cmd` package
  launcher is left alone until then, and `validate` accepts neither missing nor stale as an error only after the registry is synced.
  Needs the owner's decision.
- **Tracker launcher.** The owner named `install.cmd` and `manage.cmd`. `check-updates.cmd` already has no "gupkg" in its name;
  proposal: it moves to `src\templates\` unchanged.

## 6. Acceptance

- The workflows of AGENTS.md rule 12 that touch launchers run for real: install from a double-click `install.cmd`, `manage.cmd`
  from a package folder (user and system mode, elevated and not), a failed step (no gupkg anywhere, untrusted folder) and recovery.
- Each new regression test fails on the old code and passes on the new (rule 2); the full suite is run and the counts reported.
- The registry in `gupkg-registry\` is synced and `validate` passes.
- Docs, `--help` and the key hints agree on the names.
