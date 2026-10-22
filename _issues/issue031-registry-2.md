# Issue 031: Registry 2 — pinned versions, trackers, CI autoupdate, package migration

Date: 2026-10-22
Priority: High
Epic: 020. Design: `_docs/rehaul_design.md` §17, §8. Repository: `guraltsev/gupkg-registry`
Continues issue 014 (new packages), which is now written against this layout.

## Problem

- **Packages ship only as a seed.** 78 of the 79 packages are a `_bootstrap`
  seed; qbittorrent alone ships a versioned directory (`5.2.3/`). Installing
  always runs an upstream check on the user's machine.
- **Many packages skip checksums.** 35 set `ignore_checksum = true`.
  The user therefore gets unverified payloads, or must use
  `--no-checksum`, which the owner's new default refuses.
- **The hooks are duplicated and buggy.** There are 62 hook files, and the
  bugs are listed in issue 026.
- **Packages with portable settings lose their settings on update**, whatever
  `only_portable` says.
  - Settings beside the executable, per the registry: tixati and SumatraPDF
    (both declare `only_portable = true`).
  - Settings beside the executable, per upstream behaviour (unverified):
    Notepad++, HWiNFO, CrystalDisk*, Rufus, and probably KeePassXC.
  - Switchable to portable: vscode, obsstudio, firefox, winscp, winmerge.
- **A few package-specific faults:**
  - qbittorrent's `pkg.toml` contains template leftovers and a forward slash
    in `$App/qbittorrent.exe`;
  - zotero keeps NSIS debris in `App`;
  - msys2 runs the vendor SFX instead of 7-Zip;
  - the `nano` and `zotero` docstrings describe the wrong archive type.

## Scope

1. **Layout** as in §17: each package has `<name>/_check_updates/` and the
   newest N=2 pinned `<name>/<version>/`, plus a `_registry.toml` at the top.
2. **Convert every package to format 2** with `gupkg pkg migrate` (issue 032),
   then rewrite it by hand onto the services (issue 026).
   - The target is at most 13 remaining hook files, each with a
     recorded reason.
   - The package launchers `install.cmd` and `gupkg-manager.cmd`, generated
     from the template of issue 033, replace `gupkg-tui.cmd`. Registry
     packages do not bundle gupkg.
3. **Declare `modes` and `[portable]` / `[nonportable]` per package.**
   - **Portable-capable:** tixati, SumatraPDF, notepadplusplus, hwinfo,
     crystaldiskinfo, crystaldiskmark, rufus, keepassxc, winscp, winmerge,
     vscode (`data/`), obsstudio (`portable_mode.txt`), firefox (`-profile`),
     keypirinha (`portable/`), and systeminformer (settings XML).
   - **Verify each package's upstream portable mechanism first.** The
     analysis inferred several of them without testing.
4. **CI.** These run as GitHub Actions on a Windows runner:
   - **validate:** on every push, `gupkg pkg check` over every package, plus
     the tracker fixtures;
   - **autoupdate:** daily, and also on manual dispatch. It runs every tracker
     and, for each new version:
     1. downloads the artifact;
     2. verifies it against the discovered checksum, or computes one with
        `sha256_from = "registry-ci"`;
     3. runs unpack and prepare in `--sandbox`;
     4. commits `<name>/<version>/` and prunes versions beyond N;
     5. opens a pull request for review instead of pushing to `main`, when
        the package is marked `review = true` or the hook code changed.
5. **Remove the bypasses.** `ignore_checksum` is removed everywhere. Every
   pinned version carries a `sha256`, and the autoupdate job guarantees it.
6. **Fix the package-specific faults** listed above as each package is
   converted.
7. **The registry's own tests** move to the format 2 validator. The `_tests`
   and `_tools` modules stop importing internal gupkg modules
   (`configuration`, `core`, `registry`) and use the public `gupkg pkg check`.

## Acceptance criteria

- [ ] All 79 packages pass `gupkg pkg check`, with format 2 and no
      `ignore_checksum`.
- [ ] Every package installs on a clean Windows runner in user mode, with
      the strict CLI default (no `--skip-checksum`), under `--sandbox`.
      Portable-capable packages also install in portable mode.
- [ ] The autoupdate job, run against the recorded fixtures, produces exactly
      the expected new version directories.
- [ ] No more than 13 hook files remain, each with a "why no service" note
      in its docstring.
- [ ] Under `--sandbox`, `gupkg install <name>` and `gupkg update <name>` for
      a registry package fetch only URLs written in the selected version's
      `pkg.toml` (D10, D11). Upstream checks happen only in CI
      autoupdate, or when the user runs `gupkg check <name>` locally.
