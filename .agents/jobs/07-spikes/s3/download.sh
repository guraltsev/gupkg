#!/usr/bin/env bash
# Spike S3: download the current installers resolved from the 0.17 registry
# check_update modules (resolved 2026-10-22). Downloads go to a scratch dir
# outside the working tree. Nothing downloaded is ever executed.
set -u
DL="${1:?usage: download.sh <download-dir>}"
mkdir -p "$DL"
cd "$DL" || exit 1
# NOTE: SourceForge /download URLs return an HTML interstitial page (HTTP 200)
# to browser-like User-Agents; a non-browser UA gets the 302 to a mirror.
fetch() { # name url
  echo "== $1 <- $2"
  curl -fL --retry 3 -A "Python-urllib/3.13" -o "$1" "$2"
  echo "exit=$? size=$(stat -c %s "$1" 2>/dev/null)"
}
fetch "Firefox Setup 158.0.1.exe" "https://archive.mozilla.org/pub/firefox/releases/158.0.1/win64/en-US/Firefox%20Setup%20158.0.1.exe"
fetch "Obsidian-1.14.4.exe" "https://github.com/obsidianmd/obsidian-releases/releases/download/v1.14.4/Obsidian-1.14.4.exe"
fetch "pidgin-2.14.14-offline.exe" "https://sourceforge.net/projects/pidgin/files/Pidgin/2.14.14/pidgin-2.14.14-offline.exe/download"
fetch "qbittorrent_5.3.0_x64_setup.exe" "https://sourceforge.net/projects/qbittorrent/files/qbittorrent-win32/qbittorrent-5.3.0/qbittorrent_5.3.0_x64_setup.exe/download"
fetch "Zotero-10.0.6_x64_setup.exe" "https://download.zotero.org/client/release/10.0.6/Zotero-10.0.6_x64_setup.exe"
fetch "gimp-3.2.6-setup.exe" "https://download.gimp.org/gimp/v3.2/windows/gimp-3.2.6-setup.exe"
fetch "msys2-base-x86_64-20260927.sfx.exe" "https://github.com/msys2/msys2-installer/releases/download/2026-09-27/msys2-base-x86_64-20260927.sfx.exe"
sha256sum ./*
