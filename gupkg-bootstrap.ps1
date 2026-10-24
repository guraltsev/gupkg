<#
.SYNOPSIS
    Installs gupkg itself, then sets up manager mode, in one step.

.DESCRIPTION
    Downloads (or copies) the self-contained gupkg release ZIP, verifies its
    SHA-256, checks every file against the release's MANIFEST.sha256, places it
    at <Root>\gupkg\<version>, and runs "gupkg install" on it so the
    'current' junction, the gupkg / gupkg-tui commands, and PATH are created.
    It then runs "gupkg manager init" to write a default gupkg-config.toml (in the gupkg folder, admin-only for a system install) and
    create the package folders. Every step is safe to repeat: an existing
    version folder is reused, and install re-applies the Windows integration.

    Needs Windows PowerShell 5.1 or newer. No Python, Git, or pip is required in
    advance: the release ZIP carries its own Python, the locked dependencies and
    the native launchers. The only downloads are RELEASES.md and the ZIP.

.PARAMETER Scope
    auto (default): system when the shell is elevated, otherwise user.
    user: per-user, no elevation. system: all users, needs an elevated
    PowerShell; the gupkg package folder is then restricted so that only
    Administrators and SYSTEM can modify it, as elevated gupkg runs require.

.PARAMETER Source
    A release ZIP (https:// URL or local path) or an extracted release folder,
    as produced by tools\build_standalone.py. By default the newest release
    in RELEASES.md of the official repository is installed, checked against the
    SHA-256 listed in the same row.

.PARAMETER Sha256
    Expected SHA-256 of the ZIP. Needed for any source but the default one (the
    script warns when it is missing); the script stops if it does not match.

.PARAMETER Root
    Package root to install under. Defaults to %USERPROFILE%\opt for user scope
    and %SYSTEMDRIVE%\opt for system scope (the same defaults as manager init).

.PARAMETER SkipInstall
    Only place the files; do not create junction, commands, or PATH entries.

.PARAMETER SkipManagerInit
    Do not create a default manager configuration.

.PARAMETER Force
    Replace an existing <Root>\gupkg\<version> folder instead of reusing it.

.EXAMPLE
    .\gupkg-bootstrap.ps1
    Installs gupkg for the current user and sets up manager mode.

.EXAMPLE
    .\gupkg-bootstrap.ps1 -Scope system -Source C:\drop\gupkg-0.18.0-win-amd64.zip -Sha256 <digest>
    Installs a vetted release bundle for all users (run from an elevated shell).
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'user', 'system')]
    [string]$Scope = 'auto',
    [string]$Source,
    [string]$Sha256,
    [string]$Root,
    [switch]$SkipInstall,
    [switch]$SkipManagerInit,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Older Windows PowerShell defaults to protocols GitHub rejects.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Step([string]$Message) { Write-Host "[gupkg] $Message" }

function Get-LatestRelease {
    # RELEASES.md lists releases newest first:
    # | version | `commit` | date | sha256 | notes |
    $url = 'https://raw.githubusercontent.com/guraltsev/gupkg/main/RELEASES.md'
    Step "Reading $url"
    $text = (Invoke-WebRequest -UseBasicParsing -Uri $url).Content
    $found = [regex]::Match($text, '(?m)^\|\s*(\d+(?:\.\d+)*)\s*\|\s*`?([0-9a-fA-F]{40})`?\s*\|[^|]*\|\s*`?([0-9a-fA-F]{64})`?\s*\|')
    if (-not $found.Success) { throw "RELEASES.md lists no release with a SHA-256 digest." }
    $version = $found.Groups[1].Value
    Step "Latest release: $version ($($found.Groups[2].Value.Substring(0, 7)))"
    return [pscustomobject]@{
        Url    = "https://github.com/guraltsev/gupkg/releases/download/v$version/gupkg-$version-win-amd64.zip"
        Sha256 = $found.Groups[3].Value
    }
}

function Expand-ZipSafely([string]$Archive, [string]$Destination) {
    # Windows PowerShell 5.1's Expand-Archive does not guard '..' entries, so
    # every entry is checked to stay below the destination before extracting.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $root = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw "The archive contains an unsafe path: $($entry.FullName)" }
            if ($entry.FullName.EndsWith('/')) { New-Item -ItemType Directory -Force -Path $target | Out-Null; continue }
            New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    } finally { $zip.Dispose() }
}

function Test-ReleaseManifest([string]$Folder, [switch]$InstalledCopy) {
    # The release carries gupkg\MANIFEST.sha256: "<sha256>  <relative path>" for every
    # other file of the version folder. Every file must match, and none may be
    # missing or extra; a payload that fails is never installed.
    $manifestPath = Join-Path $Folder 'gupkg\MANIFEST.sha256'
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw "The release has no gupkg\MANIFEST.sha256; reinstall from an official release ZIP." }
    $expected = @{}
    foreach ($line in [IO.File]::ReadAllLines($manifestPath)) {
        $entry = [regex]::Match($line, '^([0-9a-f]{64})  (\S(?:.*\S)?)$')
        if (-not $entry.Success) { throw "Malformed line in MANIFEST.sha256: $line" }
        $relative = $entry.Groups[2].Value
        if ($expected.ContainsKey($relative)) { throw "Duplicate path in MANIFEST.sha256: $relative" }
        $parts = $relative -split '/'
        if ($relative.StartsWith('/') -or $relative.Contains(':') -or $relative.Contains('\') -or ($parts -contains '..') -or ($parts -contains '')) {
            throw "Unsafe path in MANIFEST.sha256: $relative"
        }
        $expected[$relative] = $entry.Groups[1].Value
    }
    $base = (Get-Item -LiteralPath $Folder).FullName.TrimEnd('\')
    $actual = @{}
    foreach ($file in Get-ChildItem -LiteralPath $Folder -Recurse -File -Force) {
        $relative = $file.FullName.Substring($base.Length + 1).Replace('\', '/')
        if ($relative -eq 'gupkg/MANIFEST.sha256') { continue }
        # An installed copy also holds installation state, which is not release content.
        if ($InstalledCopy -and ($relative -match '^(App|Icons|Shortcuts)/' -or $relative -eq 'gupkg/gupkg-config.toml')) { continue }
        $actual[$relative] = $file.FullName
    }
    $modified = @($expected.Keys | Where-Object { $actual.ContainsKey($_) -and (Get-FileHash -Algorithm SHA256 -LiteralPath $actual[$_]).Hash.ToLower() -ne $expected[$_] } | Sort-Object)
    $missing = @($expected.Keys | Where-Object { -not $actual.ContainsKey($_) } | Sort-Object)
    $extra = @($actual.Keys | Where-Object { -not $expected.ContainsKey($_) } | Sort-Object)
    if ($modified.Count + $missing.Count + $extra.Count -gt 0) {
        $first = @($modified | ForEach-Object { "modified: $_" }) + @($missing | ForEach-Object { "missing: $_" }) + @($extra | ForEach-Object { "extra: $_" })
        throw "The release does not match its MANIFEST.sha256 ($($modified.Count) modified, $($missing.Count) missing, $($extra.Count) extra; first: $($first[0]))."
    }
    Step "Checked $($expected.Count) files against MANIFEST.sha256"
}

function Find-GupkgFolder([string]$Folder) {
    # A gupkg folder holds pkg.toml and gupkg\cli.py; choose the shallowest.
    $candidates = @(Get-Item -LiteralPath $Folder) + @(Get-ChildItem -LiteralPath $Folder -Recurse -Directory)
    $found = $candidates |
        Where-Object { (Test-Path (Join-Path $_.FullName 'pkg.toml')) -and (Test-Path (Join-Path $_.FullName 'gupkg\cli.py')) } |
        Sort-Object { $_.FullName.Length } |
        Select-Object -First 1
    if (-not $found) { throw "No gupkg folder (pkg.toml plus gupkg\cli.py) was found in: $Source" }
    return $found.FullName
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("gupkg-bootstrap-" + [Guid]::NewGuid().ToString('N'))
try {
    # 1. Decide where gupkg goes, and refuse early when the scope cannot work.
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($Scope -eq 'auto') {
        $Scope = if ($isAdmin) { 'system' } else { 'user' }
        $why = if ($isAdmin) { 'detected: elevated shell' } else { 'detected: non-elevated shell' }
    } else {
        $why = 'requested with -Scope'
    }
    if ($Scope -eq 'system' -and -not $isAdmin) {
        throw "System scope needs an elevated PowerShell. Re-run as Administrator, or use -Scope user."
    }
    if (-not $Root) {
        $Root = if ($Scope -eq 'system') { Join-Path ($env:SystemDrive + '\') 'opt' } else { Join-Path $env:USERPROFILE 'opt' }
    }
    $scopeNote = if ($Scope -eq 'system') { 'all users' } else { 'current user only' }
    Step "Install scope: $Scope ($scopeNote; $why); package root: $Root"
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    if (-not $Source) {
        $latest = Get-LatestRelease
        $Source = $latest.Url
        $Sha256 = $latest.Sha256
    }

    # 2. Get the archive (or folder) and verify it before unpacking anything.
    if (Test-Path -LiteralPath $Source -PathType Container) {
        Step "Using folder $Source"
        $tree = $Source
    } else {
        $archive = Join-Path $work 'gupkg.zip'
        if (Test-Path -LiteralPath $Source) {
            Step "Using archive $Source"
            Copy-Item -LiteralPath $Source -Destination $archive
        } elseif ($Source -match '^https?://') {
            Step "Downloading $Source"
            Invoke-WebRequest -UseBasicParsing -Uri $Source -OutFile $archive
        } else {
            throw "Source not found (expected an http(s) URL, a ZIP file, or a folder): $Source"
        }
        if ($Sha256) {
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
            if ($actual -ne $Sha256) { throw "SHA-256 mismatch: expected $Sha256 but the archive is $actual" }
            Step 'SHA-256 verified'
        } else {
            Write-Warning 'No -Sha256 given: the download is not verified.'
        }
        $tree = Join-Path $work 'unpacked'
        Expand-ZipSafely $archive $tree
    }

    # 3. Place it at <Root>\gupkg\<version>, reusing an existing copy. The release
    #    must carry its own Python and match its manifest exactly.
    $payload = Find-GupkgFolder $tree
    if (-not (Test-Path -LiteralPath (Join-Path $payload 'gupkg\python\python.exe'))) {
        throw "This is not a self-contained gupkg release: gupkg\python\python.exe is missing. Use a release ZIP."
    }
    Test-ReleaseManifest $payload
    # The runtime module is the one place the release number is declared.
    $versionModule = Get-Content -LiteralPath (Join-Path $payload 'gupkg\_version.py') -Raw
    if ($versionModule -notmatch '(?m)^__version__\s*=\s*"([^"]+)"') { throw "gupkg\_version.py in $payload declares no version." }
    $version = $Matches[1]
    $target = Join-Path (Join-Path $Root 'gupkg') $version
    if ((Test-Path -LiteralPath $target) -and $Force) {
        Step "Replacing $target"
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    if (Test-Path -LiteralPath $target) {
        Test-ReleaseManifest $target -InstalledCopy
        Step "Version $version is already at $target; reusing it"
    } else {
        Step "Installing gupkg $version to $target"
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
        Copy-Item -LiteralPath $payload -Destination $target -Recurse
    }
    # A system-scope install is run elevated, and elevated runs execute only code
    # that non-administrators cannot modify. Restrict the gupkg package folder
    # (so every later version folder inherits it) to SYSTEM and Administrators
    # with write access plus Users with read and execute access, make
    # Administrators the owner, and stop if that cannot be done. Do not add /T to
    # the inheritance/grant command: it strips the inherited entries from files
    # and leaves them with no access list at all (even Administrators are denied).
    # Children pick the new entries up through inheritance instead.
    if ($Scope -eq 'system') {
        $guarded = Split-Path $target
        Step "Restricting $guarded to administrators (read-only for other users)"
        & icacls.exe $guarded /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not set the owner of $guarded (icacls exit $LASTEXITCODE)." }
        & icacls.exe $guarded /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not restrict $guarded (icacls exit $LASTEXITCODE)." }
    }
    $launcher = Join-Path $target 'gupkg.cmd'
    if (-not (Test-Path -LiteralPath $launcher)) { throw "Missing launcher: $launcher" }
    if ($SkipInstall) {
        Step "Files are in place. Finish later with: `"$launcher`" --scope $Scope install `"$target`""
        return
    }

    # The manager configuration lives in the gupkg folder of each version (never in
    # a user-writable place). Carry the active version's file into a new version
    # before the junction moves.
    $newConfig = Join-Path $target 'gupkg\gupkg-config.toml'
    $activeConfig = Join-Path (Join-Path (Split-Path $target) 'current') 'gupkg\gupkg-config.toml'
    if (-not (Test-Path -LiteralPath $newConfig) -and (Test-Path -LiteralPath $activeConfig)) {
        Step 'Keeping the manager configuration of the active version'
        Copy-Item -LiteralPath $activeConfig -Destination $newConfig
    }

    # 4. Activate: current junction, gupkg / gupkg-tui commands, PATH.
    Step 'Running gupkg install'
    & $launcher --scope $Scope install $target
    if ($LASTEXITCODE -ne 0) { throw "gupkg install failed (exit $LASTEXITCODE)." }

    # 5. Create the default manager configuration, unless one already exists.
    if (-not $SkipManagerInit) {
        if (Test-Path -LiteralPath $newConfig) {
            Step 'A manager configuration already exists; leaving it unchanged'
        } else {
            Step 'Setting up manager mode (configuration is stored in the gupkg folder)'
            & $launcher manager init
            # gupkg 1.0 has no "manager init": the command forwards to a gupkg-manager that is not part of this gupkg yet.
            # Issue 033 replaces this bootstrap; until then a missing manager init is a warning, not a failure.
            if ($LASTEXITCODE -ne 0) { Step "Warning: 'gupkg manager init' is not available in gupkg 1.0 (exit $LASTEXITCODE); skipping it." }
        }
    }
    Step 'Done. Open a NEW terminal, then try:  gupkg --help'
    Step 'Next: gupkg manager registry sync'
}
catch {
    [Console]::Error.WriteLine("[gupkg] ERROR: $($_.Exception.Message)")
    exit 1
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
