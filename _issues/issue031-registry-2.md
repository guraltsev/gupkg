# Issue 031: Registry 2 — pinned versions, trackers, registry tool, package migration

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
  - msys2 runs the vendor SFX instead of 7-Zip (its SFX uses Zstandard,
    which the bundled 7-Zip cannot open);
  - the `nano` and `zotero` docstrings describe the wrong archive type.

## Scope

1. **Layout** as in §17: each package has `<name>/_check_updates/` and its
   pinned `<name>/<version>/` directories, plus a `_registry.toml` at the
   top. **The registry keeps every version** (D36); deleting one is a
   manual maintainer action.
   - **Every pinned version carries a fingerprint** (D29): one SHA-256 over
     the version directory's files, published in the registry so that an
     install from a distribution folder can be checked against it.
2. **Convert every package to format 2** with the separate migration tool
   (issue 023, D33), then rewrite it by hand onto the services (issue 026).
   - The target is at most 13 remaining hook files, each with a
     recorded reason.
   - The package launchers `install.cmd` and `gupkg-tui.cmd` are generated
     from the template of issue 033 (D34). Registry packages do not bundle
     gupkg.
   - **Unpack kinds** (D39): msys2 moves to its `.tar.xz` release with the
     `tar` kind; GIMP keeps running its vendor installer (which the engine runs only from a download with a pinned `sha256`: the registry tool must publish one, design 9.1 rule 6), since there is no
     `inno` kind.
3. **Declare `modes` and `[portable]` / `[nonportable]` per package.**
   - **Portable-capable:** tixati, SumatraPDF, notepadplusplus, hwinfo,
     crystaldiskinfo, crystaldiskmark, rufus, keepassxc, winscp, winmerge,
     vscode (`data/`), obsstudio (`portable_mode.txt`), firefox (`-profile`),
     keypirinha (`portable/`), and systeminformer (settings XML).
   - **Verify each package's upstream portable mechanism first.** The
     analysis inferred several of them without testing.
4. **The registry tool** (D36). It runs every tracker (or the named ones)
   and, for each new version:
   1. downloads the artifact;
   2. verifies it against the discovered checksum, or, when the vendor
      publishes none, computes one with `sha256_from = "registry"` (D8);
   3. runs unpack and prepare in `--sandbox`;
   4. writes `<name>/<version>/` with its `sha256` and its fingerprint;
   5. stops. It deletes no version.

   A maintainer reviews the new directories and pushes them. With
   `--auto-push` the tool commits and pushes directly, so a scheduled job
   can run it.
5. **CI validate.** A GitHub Actions job on a Windows runner runs, on
   every push, `gupkg pkg check` over every package, plus the tracker
   fixtures, and checks that every pinned version's fingerprint matches
   its files.
6. **Remove the bypasses.** `ignore_checksum` is removed everywhere. Every
   pinned version carries a `sha256`, and the registry tool guarantees it.
7. **Fix the package-specific faults** listed above as each package is
   converted.
8. **The registry's own tests** move to the format 2 validator. The `_tests`
   and `_tools` modules stop importing internal gupkg modules
   (`configuration`, `core`, `registry`) and use the public `gupkg pkg check`.

## Acceptance criteria

- [ ] All 79 packages pass `gupkg pkg check`, with format 2 and no
      `ignore_checksum`.
- [ ] Every package installs on a clean Windows runner in user mode, with
      the strict CLI default (no `--skip-checksum`), under `--sandbox`.
      Portable-capable packages also install in portable mode.
- [ ] The registry tool, run against the recorded fixtures, produces exactly
      the expected new version directories, each with a `sha256` and a
      fingerprint that matches its files. It keeps every older version, and
      without `--auto-push` it makes no commit and no push.
- [ ] Installing a copy of a pinned version directory as a distribution
      folder, with the registry synced, labels the install *verified against
      registry `<name>`*; after one file of the copy is changed, the install
      is refused.
- [ ] No more than 13 hook files remain, each with a "why no service" note
      in its docstring.
- [ ] Under `--sandbox`, `gupkg install <name>` and `gupkg update <name>` for
      a registry package that uses the built-in download script and no
      hooks fetch only URLs written in the selected version's `pkg.toml`
      (D11, D28). Upstream checks happen only in the registry tool, or when
      the user runs `gupkg check-updates <name>` locally.

## Notes from the offline pass (job 08)

The work was done offline in a local clone of the registry. Done: the conversion and rewrite of all 79 packages (no hook file
remains), the registry tool (`src/gupkg_registry`), the fingerprint algorithm (`gupkg.services.fingerprint`, design 17), the layout
with `_registry.toml`, the CI workflow, the registry's tests on the package checker, and the tests in this repository. Owed to the owner
(`tests/manual_smoke.md`, "Registry 2", and the registry's README): populating real pinned versions with real hashes, hashing
`qbittorrent/5.2.3` (listed under `unpinned` in `_registry.toml` until then), installing every package on a clean runner, verifying each
portable mechanism (`PORTABLE-UNVERIFIED.md`), recording the HTTP fixtures, and the engine-side use of the fingerprint
(`gupkg.services.fingerprint.verify_directory`, for issue 028).
