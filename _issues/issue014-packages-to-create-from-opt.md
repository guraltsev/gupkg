> **Status (2026-10-24):** Still open, re-targeted. New packages are written in the registry 2 layout (`<name>/_check_updates/` plus pinned `<name>/<version>/`, format 2), which issue 031 delivered. Read `pkg.bootstrap` below as `_check_updates` (0.17's `_bootstrap` is not a name 1.0 knows), and `pkgs\` as the `gupkg-registry` repository. A new package starts with `gupkg pkg new NAME --track github:OWNER/REPO` and is checked with `gupkg pkg check`; prefer the built-in services of issue 026 over `module` hooks.

# Issue 014: Packages to create in `pkgs\` from `c:\opt`

Date: 2026-10-15
Priority: Medium
Change type: New package trackers (`<name>\_check_updates\pkg.toml` in the registry; was `pkgs\<name>\pkg.bootstrap\pkg.toml`)

## Objective

`c:\opt` holds ~80 legacy package directories (old `pkg.py` / `opt_pkg.json` /
`install.cmd` layouts or hand-copied apps). Only 20 have a template in `pkgs\`.
Decide which of the rest should get a `pkg.bootstrap` template and record where
each one downloads from.

## Decisions

- Older versions kept in `c:\opt` (several `emacs`, `VLC`, `vscode` versions) are
  not interesting. Package only the latest upstream release.
- Fix the suspicious `c:\opt` versions: GeoGebra6 (`16.0.14.7295`) and rmapi
  (`v.l1`).
- `c:\opt\Tools\` utilities become one package per tool (Section C).
- There is no `web` update mode. Use `module` (see Autoupdate requirement).

## How to read this issue

- **URLs were checked on 2026-10-15** with `curl` and the GitHub releases API.
  Notes say `Verified` (asset or URL confirmed) or `UNVERIFIED` (page reachable
  but the direct download link could not be confirmed, often because the site
  blocks scripted requests). `(?)` marks a mode or payload I am guessing at.
  Blank cells are for you to fill in.
- Packages are split into **Priority 1** (do first) and **Priority 2** inside
  each section.
- `Mode` is the likely `[update.check]` mode: `github` (release asset),
  `module` (custom checker, see Autoupdate requirement), `git`, or `manual`.
- `Payload` is the likely `[update.payload]` mode: `zip`, `7z`/installer
  extract, or `module`.
- Tick `[x]` once the template exists, `gupkg install` works and
  `gupkg update --check-only` finds the latest upstream version.

## Already covered in `pkgs\` (no action)

7zip, autohotkey, backrest, emacs, ffmpeg, fnm, keepassxc, latex-toolkit,
mogan, msys2, overleaf-sync, pandoc, qbittorrent, restic, sqlitebrowser
(= `DB Browser for SQLite`), systeminformer (= `Tools\SystemInformer`,
`ProcessHacker` successor), vlc, vscode, windirstat (= `Tools\WinDirStat`), xmake.

In `pkgs\` but not in `c:\opt`: SumatraPDF-portable, resticprofile, volta.

Possible clean-ups in `c:\opt` itself: `msys` (old, superseded by `msys2`),
`Tools\ProcessHacker` (superseded by SystemInformer), `Tools\7Zip-console`
(covered by `7zip`), `nano` v7.2 (old build).

## Autoupdate requirement

Every package created from this issue must update itself. The `c:\opt` copies
are old (for example Czkawka 8.0.0 vs 12.0.2, WinSCP 6.3.5 vs 6.7,
Notepad++ 8.9.2 vs 8.9.8.1, quarto 1.8.26 vs 1.10.19), so each template must
discover the latest release rather than pin the opt version.

- Each `pkg.toml` has `[update.check]` and `[update.payload]`. The `pkg.bootstrap`
  template uses `version = "bootstrap"` and no pinned version.
- Existing templates only use these check modes: `github` (release asset, as in
  `pandoc`), `git` (as in `latex-toolkit`), and `module` (a
  `pkg.local\check_update.py` returning the version and URL, as in
  `SumatraPDF-portable` and `vlc`).
- Prefer `github` where a release asset exists. Use `module` for vendor sites
  (WinSCP, Graphviz, Python, Node, julia, testdisk, Tixati, TeXmacs, ...).
- If the vendor publishes no checksum, set `ignore_checksum = true` explicitly,
  as `SumatraPDF-portable` does.
- Not eligible, mark `manual`: sites that block scripted requests or hide the
  download behind a click (CWRsync, GeoGebra, rapidee).
  Record why in the notes and revisit if a stable URL turns up.
- A package is done only when `gupkg update --check-only` reports the current
  upstream version from the template.

## Implementation status (Priority 1)

Implemented on 2026-10-15 in `pkgs\<name>\pkg.bootstrap`: 34 packages, each with a
`description`, an `[update.check]`, and an `[update.payload]`. For every one, `config-check` passes,
`gupkg update --check-only` finds the current upstream release, and a real `--download-only` into a
scratch folder produced an `App` containing every file the shortcuts, `bin` shims, and `path` entries
point to. The tests are in `tests/test_priority1_packages.py`.

Package names are lowercase with no separators, as in the rest of `pkgs\`: `sharex`, `xournalpp`,
`gsmartcontrol`, `notepadplusplus`, `winmerge`, `obsstudio`, `nano`, `windowsterminal`, `quarto`,
`czkawka`, `paintdotnet`, `rufus`, `imagemagick`, `bulkcrapuninstaller`, `keypirinha`, `obsidian`,
`zotero`, `geogebra` (GeoGebra 6), `geogebra5`, `inkscape`, `testdisk`, `graphviz`, `winscp`,
`irfanview`, `hwinfo`, `hwmonitor`, `openhardwaremonitor`, `rapidee`, `lockhunter`, `wumgr`,
`autoruns`, `processexplorer`, `crystaldiskinfo`, `crystaldiskmark`.

Not done (reasons in the tables): Gimp, Shutter Encoder, WinaeroTweaker, GenericLogViewer.

Things learned while implementing:

- gupkg's ZIP payload cannot read Deflate64 ZIPs (nano). Those and all `.7z`/installer payloads use
  `[update.payload] mode = "module"` with a `pkg.local\unpack_app.py` that calls the bundled 7-Zip.
- `github` check mode needs the asset name to follow the tag. Bulk Crap Uninstaller (`v6.3` vs `6.3.0`)
  and WuMgr (`v1.1` vs `WuMgr_v1.1b.zip`) use a small `module` checker instead.
- Download hosts that reject the default Python User-Agent (HWiNFO, IrfanView, Inkscape) get their headers
  from the candidate's `headers` field. IrfanView also needs a `Referer`.
- Obsidian's installer is NSIS with the app in an inner `app-64.7z`; its hook unpacks both layers.
  Zotero's installer unpacks to `core\`; LockHunter's portable ZIP holds one exe, renamed by an install step.
- Sysinternals publishes unversioned ZIPs, so the version is the file's `Last-Modified` date.
- GeoGebra and Inkscape's servers answer intermittently with 502, so their checkers retry.
- Opt versions are replaced by the latest upstream release, as decided above: GeoGebra Classic 5 is now
  5.4.932.1, GeoGebra 6 is 6.0.932.1 (the opt version number was a copy-paste error).

## A. Strong candidates: free, public portable/zip downloads

### Priority 1

| Done | Package (`c:\opt` dir, version) | Source | Mode / Payload | Notes / to fill |
| ---- | ------------------------------- | ------ | -------------- | --------------- |
| [x] | inkscape (v1.4.4) | https://inkscape.org/release/ (Windows 64-bit compressed 7z) | module / 7z | UNVERIFIED: page reachable, direct archive URL not checked. |
| [x] | ImageMagick (v7.1.1-40) | https://github.com/ImageMagick/ImageMagick (`ImageMagick-${version}-portable-Q16-HDRI-x64.7z`) | github / 7z | Verified: asset exists (tag `7.1.2-34`, same format as old `7.1.1-40`). The `imagemagick.org/archive/binaries/` URL is 404. |
| [x] | ShareX (v16.1) | https://github.com/ShareX/ShareX (`ShareX-${version}-portable-x64.zip`) | github / zip | Verified: latest `v21.0.0`. |
| [x] | XournalPP (v1.2.3) | https://github.com/xournalpp/xournalpp (`xournalpp-${version}-windows-portable-AMD64.zip`) | github / zip | Verified: latest `v1.3.8`. |
| [x] | BulkCrapUninstaller (v5.9.0.0) | https://github.com/BCUninstaller/Bulk-Crap-Uninstaller (`BCUninstaller_${version}_portable.7z`) | github / 7z | Verified: repo moved from `Klocman/...` (301). Asset is `.7z`. Latest `v6.3`. |
| [x] | gsmartcontrol (v2.0.2) | https://github.com/ashaduri/gsmartcontrol (`gsmartcontrol-${version}-win64.zip`) | github / zip | Verified: latest `v2.0.2`, same as opt. Needs `smartctl` (smartmontools). |
| [x] | testdisk (v7.3) | https://www.cgsecurity.org/testdisk-${version}-WIP.win64.zip | module / zip | Verified: 200 for `testdisk-7.3-WIP.win64.zip` and `testdisk-7.2.win64.zip`. 7.3 is a WIP build. |
| [x] | Paint.NET (v5.0.13) | https://github.com/paintdotnet/release (`paint.net.${version}.portable.x64.zip`) | github / zip | Verified: asset exists (latest `v5.1.12`, ~132 MB). Use `github` mode; the website is no longer needed. |
| [x] | IrfanView (v4.67) | https://www.irfanview.com/ (zip; plugins zip is separate) | module / zip | UNVERIFIED: page reachable; zip URLs not found. Official downloads are on irfanview.info/mirrors. |
| [x] | Keypirinha (v2.26) | https://keypirinha.com/ (project is dormant; no new releases expected) | module / zip | UNVERIFIED: only the homepage was checked (200). Low priority. |
| [x] | Notepad++ (v8.9.2) | https://github.com/notepad-plus-plus/notepad-plus-plus (`npp.${version}.portable.x64.zip`) | github / zip | Verified: latest tag `v8.9.8.1`. |
| [x] | WinMerge (v2.16.42.1) | https://github.com/WinMerge/winmerge (`winmerge-${version}-x64-exe.zip`) | github / zip | Verified: latest 2.16.58.2. |
| [x] | WinSCP (v6.3.5) | https://winscp.net/eng/downloads.php (`WinSCP-${version}-Portable.zip`) | module / zip | UNVERIFIED download: page lists `WinSCP-6.7-Portable.zip` but the direct link returns an HTML page (mirror redirect). Opt has 6.3.5; current is 6.7. Also on SourceForge. |
| [x] | OBSStudio (v30.2.3) | https://github.com/obsproject/obs-studio (`OBS-Studio-${version}-Windows-x64.zip`) | github / zip | Verified: latest 32.2.2. Portable mode needs a `portable_mode.txt` file. |
| [x] | Graphviz (v14.1.2) | https://gitlab.com/api/v4/projects/4207231/packages/generic/graphviz-releases/${version}/windows_10_cmake_Release_Graphviz-${version}-win64.zip | module / zip | Verified: link pattern from https://graphviz.org/download/ (16.1.0 listed). |
| [x] | nano (v7.2) | https://github.com/okibcn/nano-for-windows (`nano-for-windows_win64_v${version}.zip`) | github / zip | Verified: latest `v7.2-22.1`. |
| [x] | WindowsTerminal (v1.24.10921.0) | https://github.com/microsoft/terminal (`Microsoft.WindowsTerminal_${version}_x64.zip`) | github / zip | Verified: latest `v1.25.2733.0`. Tags are `v<version>`. |
| [x] | quarto (v1.8.26) | https://github.com/quarto-dev/quarto-cli (`quarto-${version}-win.zip`) | github / zip | Verified: latest `v1.10.19`. Bundles its own pandoc/deno. |
| [x] | Czkawka (v8.0.0) | https://github.com/qarmin/czkawka (`windows_czkawka_gui_gtk_412.zip`) | github / zip | Verified: latest 12.0.2. Asset name has no version. Also offers Krokiet GUI: `windows_krokiet_on_windows_all_backends.zip`; pick one. |

### Priority 2

| Done | Package (`c:\opt` dir, version) | Source | Mode / Payload | Notes / to fill |
| ---- | ------------------------------- | ------ | -------------- | --------------- |
| [ ] | Node (v25.6.1) | https://nodejs.org/dist/v${version}/node-v${version}-win-x64.zip | module / zip | Verified: URL pattern returns 200 (v25.6.1). Index: https://nodejs.org/dist/index.json. Overlaps `fnm`/`volta`; keep only if wanted standalone. |
| [ ] | PortableGit (v2.46.2) | https://github.com/git-for-windows/git (`PortableGit-${version}-64-bit.7z.exe`) | github / 7z self-extract | Verified: asset exists. Tag is `v2.56.0.windows.2` but asset version is `2.56.0.2`; needs tag-to-version mapping. |
| [ ] | Python (v3.13.9) | https://www.python.org/ftp/python/${version}/python-${version}-embed-amd64.zip | module / zip | Verified: 200 for 3.13.9. Check whether the opt copy is embeddable or a full install; pip/venv support differs. |
| [ ] | julia (v1.12.6) | https://julialang-s3.julialang.org/bin/winnt/x64/1.12/julia-${version}-win64.zip | module / zip | Verified: 200 for 1.12.6. URL contains the `1.12` minor directory. |
| [ ] | komorebi (v0.1.1.30) | https://github.com/LGUG2Z/komorebi (`komorebi-${version}-x86_64-pc-windows-msvc.zip`) | github / zip | Verified: latest `v0.1.41`. Opt version `0.1.1.30` is odd. |
| [ ] | Wox (v2.3.0) | https://github.com/Wox-launcher/Wox (`wox-windows-amd64.exe`) | github / exe | Verified: release has only a single exe, not a zip (latest `v2.4.7`). Existing `pkg.modules` may hold custom logic worth reading first. |
| [ ] | npbackup (v3.0.4) | https://github.com/netinvent/npbackup (`npbackup-windows-x64-gui-public.zip`; also `-cli-public.zip`) | github / zip | Verified: latest `v3.0.4`, same as opt. Pairs with `restic`. |
| [ ] | rmapi | https://github.com/ddvk/rmapi (`rmapi-win64.zip`) | github / zip | Verified: latest `v0.0.35`. Opt version is empty (`v.l1`); fix the version. |
| [ ] | Tixati (v3.29-1) | https://tixati.com/download/windows.html | module / zip | UNVERIFIED: my guessed `download.tixati.com/...portable.zip` URL returned 404; find the real link on the page. |
| [ ] | hunspell (v1.7.2-4) | https://github.com/hunspell/hunspell (source only) | module / zip (?) | Verified: GitHub release (v1.7.5) is a source tarball only. Probably best built from MSYS2 (`mingw-w64-hunspell`); dictionaries are separate. |
| [ ] | WinDebloatTools (v20241001) | https://github.com/LeDragoX/Win-Debloat-Tools | git | Verified: repo exists but has no GitHub releases; use `git` mode. |

## B. Free, but installer-only or awkward (needs extraction recipe)

### Priority 1

| Done | Package | Source | Mode / Payload | Notes / to fill |
| ---- | ------- | ------ | -------------- | --------------- |
| [ ] | Gimp (v3.2.4) | https://www.gimp.org/downloads/ | module / Inno Setup extract (`innoextract`) | UNVERIFIED: page reachable only. No official portable. NOT DONE: GIMP 3.2.6 uses a newer Inno Setup format than `innoextract` 1.9 (the latest release) can read ("Unexpected setup loader revision"). Running the installer would write registry and uninstall state, so it was not used. Retry when `innoextract` supports it, or if gupkg gains an Inno extractor. |
| [x] | Zotero (v7.0.5) | https://www.zotero.org/download/client/dl?channel=release&platform=win-x64 | module / installer extract | Verified: returns an installer (200). No official portable; profile location needs a `-datadir` shim. |
| [x] | Obsidian (v1.6.7) | https://github.com/obsidianmd/obsidian-releases (`Obsidian-${version}.exe`) | github / NSIS extract with 7z | Verified: latest `v1.14.4`. |
| [x] | GeoGebra5 (v5.2.857.0) | https://www.geogebra.org/download | module / zip (portable) | UNVERIFIED: download page reachable; its `download.geogebra.org/package/win-port*` links returned 502 when tried. |
| [x] | GeoGebra6 (v16.0.14.7295) | https://www.geogebra.org/download | module / zip (portable) | **Version string is identical to ABBYY FineReader's, so it is almost certainly a copy-paste error. Fix (decided).** |
| [ ] | Shutter Encoder (v18.5) | https://www.shutterencoder.com/ | module / zip (?) | UNVERIFIED: page reachable only. NOT DONE: the Windows installer is `https://www.shutterencoder.com/Shutter%20Encoder%20<version>%20Windows%2064bits.exe` (20.4, linked from the home page, 166 MB) but it is an Inno Setup file `innoextract` 1.9 cannot read, as for GIMP. The GitHub repository `paulpacifico/shutter-encoder` has no release assets. |

### Priority 2

| Done | Package | Source | Mode / Payload | Notes / to fill |
| ---- | ------- | ------ | -------------- | --------------- |
| [ ] | TeXmacs (v2.1.4) | https://www.texmacs.org/Download/ftp/tmftp/windows/TeXmacs-${version}.exe | module / installer extract | Verified: `TeXmacs-2.1.4.exe` is linked from the download page (2.1.5 also listed). |
| [ ] | TeXmacs-mingw (v2.1.4.14315M) | https://github.com/slowphil/mingw-w64-texmacs (`texmacs_portable.${version}.7z.exe`) | github / 7z self-extract | Verified: tag is `svn14315+extras`, asset version `2.1.4.14315M`. Confirm this is still the intended mingw source. |
| [ ] | Pidgin (v2.14.13) | https://pidgin.im/download/windows/ (portable zip on SourceForge) | module / zip | UNVERIFIED: page reachable; could not find the portable zip link. |
| [ ] | CWRsync (v6.3.1) | https://itefix.net/cwrsync | manual | UNVERIFIED: page reachable; guessed direct zip URL returned 404. Likely needs a manual click (free client). |
| [ ] | DeltaCopy (v1.4) | (none; the original URL is dead) | manual | `aboutmyx.com/DeltaCopy.html` returned 404. Abandoned; consider dropping. |
| [ ] | Mozilla Firefox (v141-autoupdate) | https://www.mozilla.org/firefox/all/ | module / installer extract | UNVERIFIED: page reachable only. Name says autoupdate; decide whether gupkg manages updates here. |
| [ ] | RemarkableApp (v3.15.1-autoupdate) | https://remarkable.com/download | module / installer | UNVERIFIED: page reachable only. |
| [ ] | TexLive (v2024-20241003) | https://tug.org/texlive/acquire.html | module / `install-tl-windows` | UNVERIFIED: page reachable only. Very large; probably a one-time-install package or keep local. |
| [ ] | Wolfram (v14.1) | https://www.wolfram.com/ | manual | Licensed and huge. Probably local only. |

## C. `c:\opt\Tools\` utilities (one package per tool)

Each is a subfolder of `Tools`. Decision: one package per tool, not a single
`tools` collection (gupkg's collection mode, issue010, stays available if this
is revisited). The same autoupdate rule applies; the Mode column is omitted
because most tools use `module`.

### Priority 1

| Done | Tool | Source | Notes / to fill |
| ---- | ---- | ------ | --------------- |
| [x] | Autoruns | https://download.sysinternals.com/files/Autoruns.zip | Verified: 200. |
| [x] | ProcessExplorer | https://download.sysinternals.com/files/ProcessExplorer.zip | Verified: 200. |
| [x] | CrystalDiskInfo | https://crystalmark.info/en/download/ | GitHub `hiyohiyo/CrystalDiskInfo` has no releases; download from crystalmark.info. UNVERIFIED zip link. Use the zip build. |
| [x] | CrystalDiskMark | https://crystalmark.info/en/download/ | GitHub `hiyohiyo/CrystalDiskMark` has no releases; download from crystalmark.info. UNVERIFIED zip link. |
| [x] | HWInfo | https://www.hwinfo.com/download/ (portable zip `https://www.hwinfo.com/files/hwi_${version}.zip`, e.g. `hwi_854.zip` = 8.54) | module / zip | Verified: the page always lists the latest version; the checker takes the `href` of the `<a>` whose text is `Local (U.S.)` (`https://www.hwinfo.com/files/hwi_854.zip`, 200). Version is the digits in the file name (`854` -> `8.54`). Needs a browser-like User-Agent (default curl agent gets 403). No checksum published: set `ignore_checksum = true`. The dappcdn.com page is not needed. |
| [x] | HWMonitor | https://www.cpuid.com/softwares/hwmonitor.html | UNVERIFIED: page reachable; my guessed zip URL returned an HTML page. |
| [x] | OpenHardwareMonitor | https://openhardwaremonitor.org/downloads/ | Dormant. Successor LibreHardwareMonitor: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor (`LibreHardwareMonitor.zip`, verified, latest `v0.9.6`). |
| [x] | Rufus | https://github.com/pbatard/rufus (`rufus-${version}p.exe`) | Verified: latest `v4.15`. |
| [x] | WUMgr | https://github.com/DavidXanatos/wumgr (`WuMgr_v${version}.zip`) | Verified: asset is `WuMgr_v1.1b.zip` (latest tag `v1.1`); version is irregular. |
| [ ] | WinaeroTweaker | https://winaero.com/winaero-tweaker/ | UNVERIFIED: page reachable; direct zip not checked. NOT DONE: the ZIP (needs a `Referer: https://winaero.com/winaero-tweaker/` header) contains only an Inno Setup installer whose files are password-encrypted, so it cannot be unpacked without running the installer. |
| [x] | rapidee | https://www.rapidee.com/en/download | UNVERIFIED: page reachable; guessed installer URL returned 404. |
| [ ] | GenericLogViewer | | Source unknown. Fill in. NOT DONE: no source known (`opt_pkg.json` has an empty `downloadUrl`). Fill in a URL to continue. |
| [x] | LockHunter | https://lockhunter.com/download.htm | UNVERIFIED: page reachable only. |

### Priority 2

| Done | Tool | Source | Notes / to fill |
| ---- | ---- | ------ | --------------- |
| [ ] | GHelper | https://github.com/seerge/g-helper (`GHelper.zip`) | Verified: latest `v0.285`. Asset name has no version. |
| [ ] | HostsFileEditor | https://github.com/scottlerch/HostsFileEditor (`HostsFileEditor-${version}-x64.zip`) | Verified: latest `v1.5.2`. |
| [ ] | NirLauncher | https://launcher.nirsoft.net/ | UNVERIFIED: page reachable only. |
| [ ] | ServiWin | https://www.nirsoft.net/utils/serviwin-x64.zip | Verified: 200 (`serviwin.zip` is the 32-bit build). May already be in NirLauncher. |
| [ ] | WinCDEmu | https://wincdemu.sysprogs.org/download/ | UNVERIFIED: page reachable only. Portable build is `PortableWinCDEmu-*.exe`. |
| [ ] | ShortcutsSearchAndReplace | | Source unknown; probably a personal script. |
| [ ] | SystemShortcuts | | Probably personal. |
| [ ] | Revo Uninstaller Pro | https://www.revouninstaller.com/ | Homepage reachable. Commercial; licence file would be local. |
| [ ] | CCleaner-Technician | https://www.ccleaner.com/ | Homepage reachable. Commercial licence; local only. |
| [ ] | Armoury Crate Uninstall Tool | https://www.asus.com/ (support page for the tool) | UNVERIFIED: only the ASUS homepage was checked. ASUS-hosted; likely `manual`. |

## D. Local only: do not add (or add only as a local, non-public template)

| Package | Reason |
| :------ | ------ |
| gupkg | This repository. |
| restic-backup-gx | Personal wrapper around restic. Possibly a private repo; fill in URL if it should be a `git` package: `________` |
| mpatch (v1.5.0) | Origin unclear (personal tool?). Fill in: `________` |
| Shortcuts, Windows-Config, fonts | Machine-specific configuration, not software. `fonts` has only a `setup` folder. |
| Netio-GUI (v1.0.8) | Origin unclear. Fill in: `________` |
| ABBYY FineReader, Foxit PDF Editor, PDF-XChange-Editor, MiniTool PartitionWizard | Commercial, licensed installers; keep local. |
| Microsoft-Activation-Scripts | Licence-circumvention tooling; recommend not packaging it in a shared registry. |

## Suggested order

1. Section A, Priority 1. Start with the `github` + `zip` packages (Notepad++,
   WinMerge, Windows Terminal, ShareX, OBS, quarto, Czkawka, BCU, XournalPP,
   nano, gsmartcontrol), which copy `pandoc` almost verbatim. Then the
   `module` ones (inkscape, Graphviz, WinSCP, testdisk, ...).
2. Section B, Priority 1 (installer-extraction recipes).
3. Section C, Priority 1.
4. Priority 2 of sections A, B and C.
