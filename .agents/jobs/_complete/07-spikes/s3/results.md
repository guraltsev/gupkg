# Spike S3: extracting registry installers with the bundled 7-Zip

Run date: 2026-10-22.

## Environment

- Windows: `[Environment]::OSVersion` = `Microsoft Windows NT 10.0.26300.0`;
  registry `DisplayVersion` = `26H2`, build `26300.9711` (Windows 11 Enterprise;
  `ProductName` reports "Windows 10 Enterprise", as usual).
- 7-Zip: repo `src/gupkg/7zip/7z.exe` = `7-Zip 26.03 (x64) : 2026-09-03`
  (mainline Igor Pavlov build, no Zstandard codec).
- Registry: 0.17 registry clone (manifests read as data only).

## Safety

- Downloads: `<scratchpad>/s3-downloads/` (new, empty dir).
- Extraction: one new, empty dir per archive under `<scratchpad>/s3-extract/<name>/`.
- Nothing downloaded or extracted was executed. Only the repo's `7z.exe` (by
  absolute path) and the official `innoextract.exe` 1.9 were run, with the
  archive as an argument. `Get-AuthenticodeSignature` was used read-only.
- All vendor checksums that exist were verified (Firefox `SHA256SUMS`, GIMP
  `SHA256SUMS`, MSYS2 GitHub digest / `.sha256`, qBittorrent SourceForge
  `best_release.json` `sha256sum`). Obsidian, Pidgin and Zotero publish none
  next to the asset; their Authenticode signatures are valid.

## Scripts (this folder)

- `download.sh <dir>`: curl downloads of the resolved URLs + `sha256sum`.
- `extract.ps1 -Downloads -ExtractRoot -LogDir [-Only]`: per package
  `7z l <archive>` and `7z x -y -o<dir> <archive>`, logs, exit codes, file counts.
- `extract_followup.ps1 -Downloads -ExtractRoot`: path-filtered extraction
  (`7z x <archive> core`, `7z x <archive> $PLUGINSDIR\app-64.7z`), exclusion
  switches (`-x!$PLUGINSDIR -x!*.pdb -x!uninst.exe`), nested archives, the MSYS2
  `.tar.xz` two-pass path, and the previous MSYS2 SFX release.

Ad-hoc commands also used: `7z l -slt` (method / codec), `7z x` of
`$PLUGINSDIR\app-64.7z`, `7z l` of `gtk.zip` / `dbgsym.zip`, a 4 MB ASCII scan of
the GIMP installer for `Inno Setup` strings, `innoextract.exe --version` and
`innoextract.exe -i <gimp installer>`.

## URL resolution

URLs were resolved exactly as each 0.17 `pkg.local/check_update.py` does:

| Package | Source used by the 0.17 tracker | Resolved URL |
|---|---|---|
| firefox | `product-details.mozilla.org` `LATEST_FIREFOX_VERSION` = 158.0.1 | `https://archive.mozilla.org/pub/firefox/releases/158.0.1/win64/en-US/Firefox%20Setup%20158.0.1.exe` |
| obsidian | GitHub latest release asset `Obsidian-${version}.exe` | `https://github.com/obsidianmd/obsidian-releases/releases/download/v1.14.4/Obsidian-1.14.4.exe` |
| pidgin | SourceForge folder listing, newest `*-offline.exe` | `https://sourceforge.net/projects/pidgin/files/Pidgin/2.14.14/pidgin-2.14.14-offline.exe/download` |
| qbittorrent | SourceForge `best_release.json` windows entry | `https://sourceforge.net/projects/qbittorrent/files/qbittorrent-win32/qbittorrent-5.3.0/qbittorrent_5.3.0_x64_setup.exe/download` |
| zotero | redirect of `zotero.org/download/client/dl?channel=release&platform=win-x64` | `https://download.zotero.org/client/release/10.0.6/Zotero-10.0.6_x64_setup.exe` |
| gimp | newest even-minor series on `download.gimp.org` | `https://download.gimp.org/gimp/v3.2/windows/gimp-3.2.6-setup.exe` |
| msys2 | newest date-tagged GitHub release, `msys2-base-x86_64-*.sfx.exe` | `https://github.com/msys2/msys2-installer/releases/download/2026-09-27/msys2-base-x86_64-20260927.sfx.exe` |

Download gotcha: SourceForge `/download` URLs return an HTML interstitial page
with HTTP 200 (about 140 KB) to a browser-like User-Agent. With a non-browser
UA (`Python-urllib/3.13`, which is what the 0.17 modules send) they 302 to a
mirror and the real file arrives. A download step must not trust HTTP 200
alone; a checksum or a magic/size check catches this.

## Per-package results

Exit codes are from the bundled 7z (`l` / `x`). "Format" is what `7z l`
reports below the outer `PE` container.

| Package | File, size, SHA-256 (prefix) | Signed | Real kind (7z `Type`, method) | `7z l` / `7z x` | Usable app tree? | Needed subfolder / nested archive | Junk to drop |
|---|---|---|---|---|---|---|---|
| firefox 158.0.1 | `Firefox Setup 158.0.1.exe`, 94 890 968 B, `3259a2f8` (matches SHA256SUMS) | Mozilla, valid | PE + 7z SFX, `LZMA:25 BCJ2`, solid | 0 / 0 (one warning: PE "Checksum error"; still exit 0, "Everything is Ok") | Yes: `core\firefox.exe` | `core/` (74 files, 372 MB). `7z x <exe> core` extracts only it (exit 0) | `setup.exe` (outside `core`). Optional inside `core`: `uninstall\helper.exe`, `maintenanceservice*.exe`, `updater.exe`, `default-browser-agent.exe` |
| obsidian 1.14.4 | `Obsidian-1.14.4.exe`, 341 331 008 B, `28662520` | Dynalist Inc, valid | PE + NSIS, `Deflate`, non-solid | 0 / 0 | Not directly: top level is only `$PLUGINSDIR\`, `$R0\`, `resources\*.ico` | Nested `$PLUGINSDIR\app-64.7z` (119.8 MB, `LZMA2:20 BCJ`). Second `7z x` gives `Obsidian.exe`, `resources\`, `locales\`, 83 files, 406 MB. Also present: `app-32.7z`, `app-arm64.7z` (electron-builder layout). `7z x <exe> $PLUGINSDIR\app-64.7z` pulls only the inner archive (exit 0) | Everything outside the inner archive: `$PLUGINSDIR` (NSIS DLLs, other-arch archives), `$R0\Uninstall Obsidian.exe`. Top-level `resources\{base,canvas,md}.ico` are file-association icons (optional) |
| pidgin 2.14.14 | `pidgin-2.14.14-offline.exe`, 32 351 744 B, `75513899` | Gary Kramlich, valid | PE + NSIS, `LZMA:23`, solid | 0 / 0 | Partly: `pidgin.exe` and DLLs at top level, but the GTK runtime is missing | Nested `$PLUGINSDIR\gtk.zip` (384 files, top folder `Gtk\`), to extract into the app root | `$PLUGINSDIR\` (incl. `dbgsym.zip`, 12 MB of debug symbols, `INetC.dll`, `nsisunz.dll`, bitmaps), `pidgin-uninst.exe.nsis` |
| qbittorrent 5.3.0 | `qbittorrent_5.3.0_x64_setup.exe`, 50 030 660 B, `864318bb` (matches SF metadata) | not signed | NSIS directly (7z detects `Nsis` at offset 0), `LZMA:26`, solid | 0 / 0 | Yes: `qbittorrent.exe`, `qt.conf`, `translations\` | none | `qbittorrent.pdb` (270 MB!), `uninst.exe`, `$PLUGINSDIR\`. With `-x!$PLUGINSDIR -x!*.pdb -x!uninst.exe` the tree is 37 files / 53 MB instead of 47 files / 324 MB |
| zotero 10.0.6 | `Zotero-10.0.6_x64_setup.exe`, 109 520 032 B, `1cf48672` | Zotero (EV), valid | PE + 7z SFX (Mozilla-style installer, not NSIS), `LZMA:24 BCJ2`, solid | 0 / 0 | Yes: `core\zotero.exe` | `core/` (86 files, 253 MB). `7z x <exe> core` works | `setup.exe`; optional `core\uninstall\`, `core\updater.exe` |
| gimp 3.2.6 | `gimp-3.2.6-setup.exe`, 190 358 080 B, `9337cccb` (matches SHA256SUMS) | Jernej Simončič, valid | Inno Setup. 7z sees only `PE` (sections, `.rsrc`, `CERTIFICATE`, one overlay blob `[0]` of 189 MB) | 0 / 0, but the "extraction" is PE sections and resources, not program files | **No** | n/a: 7z does not support Inno Setup | n/a |
| msys2 2026-09-27 | `msys2-base-x86_64-20260927.sfx.exe`, 43 117 824 B, `ad336ccc` (matches GitHub digest) | Christoph Reiter, valid | PE (`7z.sfx.exe` from **7-Zip ZS**, "26.02 ZS v1.5.7 R1") + 7z, method `Delta LZMA:20 BCJ2 04F71101`; `04F71101` is the Zstandard codec ID of the 7-Zip ZS fork | 0 / **2**: `ERROR: Unsupported Method` for 13 830 of 13 834 files; only 4 empty files written | **No** | (would be `msys64\`) | n/a |

The previous MSYS2 SFX (`2026-06-11`, `c105946e`) uses the same ZS SFX and the
same `04F71101` method, so this is not a one-off.

### MSYS2 alternative: `.tar.xz` from the same release

`https://github.com/msys2/msys2-installer/releases/download/2026-09-27/msys2-base-x86_64-20260927.tar.xz`
(42 860 696 B, `ea2f31a0`, matches the published `.sha256`):

- `7z x <file>.tar.xz` gives `msys2-base-x86_64-20260927.tar` (247 MB), exit 0.
- `7z x <file>.tar` gives `msys64\` with 13 834 files / 236 MB, exit 0, no
  warnings. The tar has no symbolic or hard links (`7z l -slt` shows none).
- `msys64\msys2.exe`, `msys64\ucrt64.exe`, `msys64\usr\bin\env.exe` are
  present. The tree matches the SFX listing: same file count and byte total.
- Needed step: strip the single `msys64\` wrapper so `$App` is the MSYS2 root.
- A `.tar.zst` exists too, but mainline 7-Zip has no zstd, so `.tar.xz` is the
  one to use.

## What the 0.17 registry manifests do (compared)

| Package | 0.17 `unpack_app.py` | Assessment |
|---|---|---|
| firefox | `7z x -y -o<scratch> <exe> core`, check `core\firefox.exe`, rename `core` to `App` | Matches the spike. Correct |
| obsidian | `7z x` the whole installer into `stageRoot\installer`, then `7z x $PLUGINSDIR\app-64.7z` into `App`, remove the scratch | Works. It extracts ~340 MB of other-arch archives needlessly; a path filter (`$PLUGINSDIR\app-64.7z`) avoids that. Leaves no junk in `App`. The scratch is not removed when the second step raises |
| pidgin | `7z x -tNsis` into `App`, `zipfile` extract `$PLUGINSDIR\gtk.zip` into `App`, delete `$PLUGINSDIR` and `pidgin-uninst.exe` | Works, but **bug**: 7z names the uninstaller `pidgin-uninst.exe.nsis`, so it is left behind. The docstring says "debug symbols" are removed; they are, only because `dbgsym.zip` sits inside `$PLUGINSDIR` |
| qbittorrent | `7z x` the whole installer into `App` (both `unpack_app.py` and the origin `populate_app.py`) | Works, but keeps 270 MB `qbittorrent.pdb`, `uninst.exe`, `$PLUGINSDIR` |
| zotero | `7z x` into `App`, flatten if a single top-level dir. Docstring calls the asset a ".7z archive" | Works: the 7z SFX yields `core\` + `setup.exe` (two entries, so no flattening). The shortcut points at `$App\core\zotero.exe`. Keeps `setup.exe`. The docstring is wrong: the asset is a 7z SFX installer like Firefox's, and the `core` extraction used for Firefox fits better |
| gimp | **Runs the installer**: `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CURRENTUSER /NOICONS /DIR=<stageApp>` | The comment "7-Zip cannot unpack Inno" is confirmed. This executes vendor code and may write an HKCU uninstall entry |
| msys2 | **Runs the SFX**: `<sfx.exe> -y -o<stageApp>`, then flattens `msys64` | Executes the downloaded file. This was necessary because mainline 7z cannot decode the ZS/zstd payload; the `.tar.xz` asset avoids running anything |

## innoextract (for an `inno` extraction kind)

- Licence: zlib licence (`LICENSE`, Copyright 2011-2020 Daniel Scharrer):
  "Permission is granted to anyone to use this software for any purpose,
  including commercial applications, and to alter it and redistribute it
  freely" (no attribution required; altered versions must be marked; the notice
  stays in source distributions). Redistribution in a bundled ZIP is permitted.
  The official Windows ZIP is statically linked and ships
  `license/{innoextract.LICENSE, Boost.LICENSE_1_0, liblzma.COPYING,
  libstdc++.COPYING3, libstdc++.COPYING.RUNTIME}.txt`. Boost (BSL-1.0), liblzma
  (public domain) and libstdc++ with the GCC runtime exception all allow binary
  redistribution, so ship that `license/` folder with it. GitHub reports the
  repo licence as `NOASSERTION` (it does not match the zlib text).
- Latest release: **1.9, 2020-08-09** (GitHub `dscharrer/innoextract`, also
  `https://constexpr.org/innoextract/`). No release since. The master CHANGELOG
  lists an unreleased "1.10 (TBD)" with Inno Setup 6.3.x and 6.4.0 support
  (commit 2025-01-12). The last push was 2025-02-06.
- Official Windows build: yes, for 1.9 only:
  `https://github.com/dscharrer/innoextract/releases/download/1.9/innoextract-1.9-windows.zip`
  (520 705 B, SHA-256 `6989342c9b026a00a72a38f23b62a8e6a22cc5de69805cf47d68ac2fec993065`,
  with a GPG `.sig` from key `ADE9653703D4ADE0E997758128555A66D7E1DEC9`; the key
  was not in the local keyring, so the signature was not verified). There is no
  official build of master.
- Inno versions: 1.9 says "Inno Setup 1.2.10 to 6.0.5" (with preliminary 6.1.0
  support). Master supports up to 6.4.0. **6.4.2+, 6.5, 6.6, 6.7 and 7.0 are not
  supported**: open, unmerged issues/PRs #197, #201, #202, #204, #205, #208,
  #210 ("6.4.2 through 7.0.2"), #212, #214 (2025-07 to 2026-08).
- GIMP 3.2.6 installer: strings show `Inno Setup Setup Data (7.0.0.3)` and
  `Inno Setup Messages (6.5.0) (u)`, so it is built with Inno Setup 7.0.x.
- Test: `innoextract.exe -i gimp-3.2.6-setup.exe` (1.9) prints
  `Warning: Unexpected setup loader revision: 2`,
  `Warning: Setup loader checksum mismatch!`,
  `Could not determine setup data version!` and exits 2. **innoextract does not
  work on GIMP**, and master would not either (it stops at 6.4.0).
- Alternative (not tested; running it was outside this spike's permission):
  `innounp` (`https://www.rathlev-home.de/tools/download/innounp.htm`, source
  `github.com/jrathlev/InnoUnpacker-Windows-GUI`) claims support for Inno Setup
  2.0.7 through 7.1.0, version 2.71.2 of 2026-10-15. Its page says GPL; GitHub
  says MIT for the repo. That must be resolved before bundling. It has no GitHub
  releases; downloads come from the author's site.

## Design implications

- `7z` kind (plain `.7z`, `.zip`, `.tar.xz` via two passes): works with the
  bundled mainline 7z.
- `sfx` kind: "7z SFX" must mean a **mainline-codec** 7z SFX (Firefox, Zotero).
  Those work, with a mandatory sub-path selector (`core`). The MSYS2 `.sfx.exe`
  is a **7-Zip ZS SFX using Zstandard** and is **unworkable with the bundled
  7z** (exit 2, nothing extracted). The options are the `.tar.xz` asset (works,
  two passes plus a wrapper strip), bundling 7-Zip ZS, or running the SFX. The
  design must not assume "any `.sfx.exe` = 7z-extractable"; codec support
  depends on the 7z build.
- `nsis` kind: works for all three NSIS cases (obsidian, pidgin, qbittorrent; and
  7z also opens raw-NSIS qbittorrent with no PE wrapper). It needs per-package
  data:
  - an optional nested archive to extract next (`$PLUGINSDIR\app-64.7z` for
    electron-builder apps, `$PLUGINSDIR\gtk.zip` for Pidgin, which merges into
    the root rather than replacing it). The nested step can be a ZIP or a 7z, so
    "nested extract via 7z" is the general form.
  - exclude/cleanup patterns: always `$PLUGINSDIR`, `$R0`, `*.nsis`
    (uninstaller stub naming), plus package-specific ones (`*.pdb`, `uninst.exe`).
    7z `-x!` excludes do this at extraction time.
- `inno` kind: **unworkable with 7z alone** (7z sees only PE sections). It is
  also **unworkable with innoextract** for current GIMP (Inno 7.0 data). Bundling
  innoextract 1.9 would cover only installers built with Inno Setup 6.1 or older.
  Options: evaluate innounp (licence and provenance to clarify), keep a
  "run installer silently" escape hatch (what 0.17 does), or drop `inno` from
  the first design iteration.
- A generic "extract, then pick a sub-path, then optionally extract a nested
  archive, then apply excludes, then strip a single wrapper dir" pipeline covers
  firefox, zotero, obsidian, pidgin, qbittorrent and msys2 (`.tar.xz`).
- Exit codes: 7z returned 0 despite a warning (Firefox PE checksum) and 2 on
  unsupported codec. Treat >= 2 as failure and verify an expected file
  (e.g. `firefox.exe`) after extraction, as 0.17 does.
- Downloads: SourceForge returns an HTML page with HTTP 200 to browser UAs.
  Require a checksum or a format/size sanity check before extraction.
