<#
.SYNOPSIS
    Installs gupkg itself, then sets up manager mode, in one step.

.DESCRIPTION
    Downloads (or copies) a gupkg archive, optionally verifies its SHA-256,
    places it at <Root>\gupkg\v<version>, and runs "gupkg install" on it so the
    'current' junction, the gupkg / gupkg-tui commands, and PATH are created.
    It then runs "gupkg manager init" to write a default gupkg-config.toml (in the gupkg folder, admin-only for a system install) and
    create the package folders. Every step is safe to repeat: an existing
    version folder is reused, and install re-applies the Windows integration.

    Needs Windows PowerShell 5.1 or newer. No Python, Git, or pip is required in
    advance: gupkg downloads a verified embedded Python the first time it runs
    when none is installed.

.PARAMETER Scope
    auto (default): system when the shell is elevated, otherwise user.
    user: per-user, no elevation. system: all users, needs an elevated
    PowerShell; the gupkg package folder is then restricted so that only
    Administrators and SYSTEM can modify it, as elevated gupkg runs require.

.PARAMETER Source
    A ZIP archive (https:// URL or local path) or a folder containing gupkg.
    Accepts a release bundle produced by tools\build_standalone.py or a
    repository archive that contains src\gupkg. By default the newest release
    in RELEASES.md of the official repository is installed.

.PARAMETER Sha256
    Expected SHA-256 of the archive. Strongly recommended for anything but the
    default source; the script stops if it does not match.

.PARAMETER Root
    Package root to install under. Defaults to %USERPROFILE%\opt for user scope
    and %SYSTEMDRIVE%\opt for system scope (the same defaults as manager init).

.PARAMETER SkipInstall
    Only place the files; do not create junction, commands, or PATH entries.

.PARAMETER SkipManagerInit
    Do not create a default manager configuration.

.PARAMETER Force
    Replace an existing <Root>\gupkg\v<version> folder instead of reusing it.

.EXAMPLE
    .\gupkg-bootstrap.ps1
    Installs gupkg for the current user and sets up manager mode.

.EXAMPLE
    .\gupkg-bootstrap.ps1 -Scope system -Source C:\drop\gupkg-0.12.0.zip -Sha256 <digest>
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

function Get-LatestReleaseSource {
    # RELEASES.md lists releases newest first: | version | `commit` | date | notes |
    $url = 'https://raw.githubusercontent.com/guraltsev/pkg/main/RELEASES.md'
    Step "Reading $url"
    $text = (Invoke-WebRequest -UseBasicParsing -Uri $url).Content
    $found = [regex]::Match($text, '(?m)^\|\s*(\d+(?:\.\d+)*)\s*\|\s*`?([0-9a-fA-F]{40})`?\s*\|')
    if (-not $found.Success) { throw "RELEASES.md lists no release." }
    Step "Latest release: $($found.Groups[1].Value) ($($found.Groups[2].Value.Substring(0, 7)))"
    return "https://github.com/guraltsev/pkg/archive/$($found.Groups[2].Value).zip"
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

function Install-BundledRuntime([string]$PackageDir) {
    # Elevated gupkg runs use only a bundled Python and never install anything,
    # so a system-scope install provisions the runtime here, from verified
    # downloads: the embeddable CPython (pinned SHA-256, the same as gupkg.cmd
    # uses) and the hash-pinned wheels listed in gupkg-deps.lock.
    $pythonUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
    $pythonSha = '4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3'
    $lock = Join-Path $PackageDir 'gupkg-deps.lock'
    if (-not (Test-Path -LiteralPath $lock)) { throw "Missing dependency lock: $lock" }
    $stage = Join-Path $work 'runtime'
    New-Item -ItemType Directory -Force -Path $stage | Out-Null

    Step 'Downloading embedded Python 3.12.10'
    $zip = Join-Path $work 'python-embed.zip'
    Invoke-WebRequest -UseBasicParsing -Uri $pythonUrl -OutFile $zip
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash -ne $pythonSha) { throw 'Downloaded Python archive failed SHA-256 verification.' }
    Expand-Archive -LiteralPath $zip -DestinationPath $stage -Force
    $pth = "python312.zip`n.`n..`nLib/site-packages`n`nimport site`n"
    [IO.File]::WriteAllText((Join-Path $stage 'python312._pth'), $pth, [Text.Encoding]::ASCII)

    # The embeddable Python has no pip. Fetch the wheel the lock pins (matched by
    # its hash, not by name), unpack it, and let it install the locked set.
    $pin = [regex]::Match((Get-Content -LiteralPath $lock -Raw), '(?m)^pip==(\S+)\s+--hash=sha256:([0-9a-f]{64})')
    if (-not $pin.Success) { throw "$lock does not pin pip." }
    $meta = Invoke-RestMethod -UseBasicParsing -Uri "https://pypi.org/pypi/pip/$($pin.Groups[1].Value)/json"
    $wheel = $meta.urls | Where-Object { $_.packagetype -eq 'bdist_wheel' -and $_.digests.sha256 -eq $pin.Groups[2].Value } | Select-Object -First 1
    if (-not $wheel) { throw 'PyPI lists no pip wheel with the locked hash.' }
    $pipZip = Join-Path $work 'pip.zip'
    Invoke-WebRequest -UseBasicParsing -Uri $wheel.url -OutFile $pipZip
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $pipZip).Hash.ToLower() -ne $pin.Groups[2].Value) { throw 'Downloaded pip wheel failed SHA-256 verification.' }
    $sitePackages = Join-Path $stage 'Lib\site-packages'
    Expand-Archive -LiteralPath $pipZip -DestinationPath $sitePackages -Force

    Step 'Installing locked dependencies (hashes required, wheels only)'
    & (Join-Path $stage 'python.exe') -I -m pip install --isolated --disable-pip-version-check --no-input --no-warn-script-location --upgrade --target $sitePackages --require-hashes --only-binary=:all: --no-deps -r $lock | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Installing the locked dependencies failed (pip exit $LASTEXITCODE)." }
    Remove-Item -LiteralPath (Join-Path $sitePackages 'bin') -Recurse -Force -ErrorAction SilentlyContinue

    $destination = Join-Path $PackageDir 'python'
    if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
    Copy-Item -LiteralPath $stage -Destination $destination -Recurse
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
    if (-not $Source) { $Source = Get-LatestReleaseSource }

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
        Expand-Archive -LiteralPath $archive -DestinationPath $tree -Force
    }

    # 3. Place it at <Root>\gupkg\v<version>, reusing an existing copy.
    $payload = Find-GupkgFolder $tree
    $manifest = Get-Content -LiteralPath (Join-Path $payload 'pkg.toml') -Raw
    if ($manifest -notmatch '(?m)^\s*version\s*=\s*"([^"]+)"') { throw "pkg.toml in $payload declares no version." }
    $version = $Matches[1]
    $target = Join-Path (Join-Path $Root 'gupkg') "v$version"
    if ((Test-Path -LiteralPath $target) -and $Force) {
        Step "Replacing $target"
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    if (Test-Path -LiteralPath $target) {
        Step "Version $version is already at $target; reusing it"
    } else {
        Step "Installing gupkg $version to $target"
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
        Copy-Item -LiteralPath $payload -Destination $target -Recurse
    }
    # A system-scope install runs elevated, which needs the bundled runtime.
    $packageDir = Join-Path $target 'gupkg'
    if ($Scope -eq 'system' -and -not (Test-Path -LiteralPath (Join-Path $packageDir 'python\python.exe'))) {
        Install-BundledRuntime $packageDir
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
        Step 'Setting up manager mode (configuration is stored in the gupkg folder)'
        & $launcher manager init
        if ($LASTEXITCODE -eq 2) {
            Step 'A manager configuration already exists; leaving it unchanged'
        } elseif ($LASTEXITCODE -ne 0) {
            throw "gupkg manager init failed (exit $LASTEXITCODE)."
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
