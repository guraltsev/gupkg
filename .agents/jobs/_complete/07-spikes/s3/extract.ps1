# Spike S3: list and extract each downloaded installer with the repo's bundled
# 7z.exe. Each archive goes into its own new, empty directory. Nothing that was
# downloaded or extracted is executed; only 7z.exe is run.
param(
    [Parameter(Mandatory)] [string] $Downloads,
    [Parameter(Mandatory)] [string] $ExtractRoot,
    [Parameter(Mandatory)] [string] $LogDir,
    [string[]] $Only = @()
)
# 7z writes errors to stderr; keep going so the exit code is recorded.
$ErrorActionPreference = 'Continue'
$SevenZip = Join-Path $PSScriptRoot '..\..\..\..\..\src\gupkg\7zip\7z.exe'
$items = [ordered]@{
    firefox     = 'Firefox Setup 158.0.1.exe'
    obsidian    = 'Obsidian-1.14.4.exe'
    pidgin      = 'pidgin-2.14.14-offline.exe'
    qbittorrent = 'qbittorrent_5.3.0_x64_setup.exe'
    zotero      = 'Zotero-10.0.6_x64_setup.exe'
    gimp        = 'gimp-3.2.6-setup.exe'
    msys2       = 'msys2-base-x86_64-20260927.sfx.exe'
}
New-Item -ItemType Directory -Force $LogDir | Out-Null
foreach ($name in $items.Keys) {
    if ($Only.Count -and $name -notin $Only) { continue }
    $archive = Join-Path $Downloads $items[$name]
    $out = Join-Path $ExtractRoot $name
    if (Test-Path $out) { throw "$out already exists; refusing to reuse it" }
    New-Item -ItemType Directory -Force $out | Out-Null

    & $SevenZip l $archive *> (Join-Path $LogDir "$name.l.txt")
    $listExit = $LASTEXITCODE
    & $SevenZip x -y "-o$out" $archive *> (Join-Path $LogDir "$name.x.txt")
    $extractExit = $LASTEXITCODE
    $files = @(Get-ChildItem -LiteralPath $out -Recurse -File -Force)
    $bytes = ($files | Measure-Object Length -Sum).Sum
    "{0,-12} l={1} x={2} files={3} bytes={4}" -f $name, $listExit, $extractExit, $files.Count, $bytes
}
