# Spike S3 follow-up: nested archives, path-filtered extraction, and MSYS2
# alternatives. Only the repo's bundled 7z.exe is run; every output goes into a
# new, empty directory under the extract root.
param(
    [Parameter(Mandatory)] [string] $Downloads,
    [Parameter(Mandatory)] [string] $ExtractRoot
)
$ErrorActionPreference = 'Continue'
$Z = Join-Path $PSScriptRoot '..\..\..\..\src\gupkg\7zip\7z.exe'

function New-Out([string] $name) {
    $out = Join-Path $ExtractRoot $name
    if (Test-Path $out) { throw "$out already exists; refusing to reuse it" }
    New-Item -ItemType Directory $out | Out-Null
    $out
}
function Show([string] $label, [string] $out) {
    $files = @(Get-ChildItem -LiteralPath $out -Recurse -File -Force)
    "{0,-28} exit={1} files={2} bytes={3}" -f $label, $LASTEXITCODE, $files.Count, ($files | Measure-Object Length -Sum).Sum
}

# Path-filtered extraction straight from the installer (no full unpack).
$out = New-Out 'firefox-core-only'
& $Z x -y "-o$out" (Join-Path $Downloads 'Firefox Setup 158.0.1.exe') 'core' *> $null
Show 'firefox: x ... core' $out

$out = New-Out 'obsidian-inner-only'
& $Z x -y "-o$out" (Join-Path $Downloads 'Obsidian-1.14.4.exe') '$PLUGINSDIR\app-64.7z' *> $null
Show 'obsidian: x ... app-64.7z' $out

$out = New-Out 'zotero-core-only'
& $Z x -y "-o$out" (Join-Path $Downloads 'Zotero-10.0.6_x64_setup.exe') 'core' *> $null
Show 'zotero: x ... core' $out

# Excluding junk at extraction time (-x! switches) for NSIS installers.
$out = New-Out 'qbittorrent-filtered'
& $Z x -y "-o$out" (Join-Path $Downloads 'qbittorrent_5.3.0_x64_setup.exe') '-x!$PLUGINSDIR' '-x!*.pdb' '-x!uninst.exe' *> $null
Show 'qbittorrent: x -x!junk' $out

# Previous MSYS2 SFX release, to see whether the codec change is recent.
& $Z l -slt (Join-Path $Downloads 'msys2-base-x86_64-20260611.sfx.exe') |
    Select-String '^(Method|Blocks) = .+' | Select-Object -First 2 | ForEach-Object { "msys2 20260611 sfx: $($_.Line)" }
& $Z l -slt (Join-Path $Downloads 'msys2-base-x86_64-20260611.sfx.exe') |
    Select-String '^Comment|7-Zip ZS|ProductName' | Select-Object -First 3 | ForEach-Object { "msys2 20260611 sfx: $($_.Line)" }

# MSYS2 .tar.xz: two 7z passes (xz -> tar -> tree).
$tarDir = New-Out 'msys2-tarxz-stage1'
& $Z x -y "-o$tarDir" (Join-Path $Downloads 'msys2-base-x86_64-20260927.tar.xz') *> $null
Show 'msys2 tar.xz -> tar' $tarDir
$out = New-Out 'msys2-tarxz'
$tar = Get-ChildItem -LiteralPath $tarDir -Filter *.tar | Select-Object -First 1
& $Z x -y "-o$out" $tar.FullName *> (Join-Path $ExtractRoot 'msys2-tarxz.log')
Show 'msys2 tar -> tree' $out
