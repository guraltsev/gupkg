<#
.SYNOPSIS
    Installs gupkg itself (or any distribution) from a release ZIP, by running its install.cmd.

.DESCRIPTION
    The bootstrap is thin: it has no install logic of its own. It downloads (or copies) a release ZIP, verifies its
    SHA-256, unpacks it, and runs the install.cmd of the version directory it holds. That launcher starts the gupkg that
    the ZIP carries, which installs the package like any other: it copies the bundled gupkg into App, checks it against
    MANIFEST.sha256, creates the 'current' junction, the gupkg / gupkg-tui / gupkg-manager commands, the PATH entry and
    the Start Menu entry, and writes the root data folder <root>\.gupkg with the forwarder that the package launchers of
    that root use to find the installed gupkg.

    User scope: the ZIP is unpacked to a staging folder and its install.cmd runs from there, not elevated; gupkg copies the
    version directory into %USERPROFILE%\opt\gupkg\<version> and the staging folder can be deleted afterwards.

    System scope: the script must be run by an administrator in an elevated PowerShell. It creates C:\opt owned by
    Administrators and writable by nobody else (or refuses when an existing C:\opt is not like that), unpacks the ZIP
    straight into it, and runs the install.cmd of C:\opt\gupkg\<version>. Run elevated, that launcher first checks that
    everything it will run is admin-only, and refuses otherwise.

    Needs Windows PowerShell 5.1 or newer. No Python, Git or pip is needed in advance: the release carries its own Python,
    the locked dependencies and the native launchers. The only downloads are RELEASES.md and the ZIP. Re-running the
    script is also how a 0.18 installation moves to 1.0.

    Run with -Source <url or ZIP> and -Sha256, the same script installs any other program's distribution (a ZIP made
    with 'gupkg pkg export') in user scope; that installs only that program, not gupkg.

    A ZIP that was downloaded with a browser carries the Mark of the Web. Windows then asks before it runs the first
    install.cmd ("Open File - Security Warning"): click Run, or right-click the ZIP, choose Properties and tick Unblock
    before extracting it. What this script unpacks itself carries no mark, and gupkg's copy of a distribution into a root
    drops the mark from every file it copies.

.PARAMETER Scope
    auto (default): system when the shell is elevated, otherwise user.
    user: for the current user, run from a shell that is not elevated (gupkg never acts on a user root as administrator).
    system: for all users, needs an elevated PowerShell.

.PARAMETER Source
    A release ZIP (https:// URL or local path), or in user scope an extracted folder that holds an install.cmd. By
    default the newest release in RELEASES.md of the official repository is installed, checked against the SHA-256 listed in
    the same row.

.PARAMETER Sha256
    Expected SHA-256 of the ZIP. Needed for any source but the default one (the script warns when it is missing); the
    script stops if it does not match.

.PARAMETER Root
    The system root (system scope only). Defaults to %SYSTEMDRIVE%\opt.

.PARAMETER Stage
    User scope: unpack into this folder instead of a temporary one. The folder is kept.

.PARAMETER SkipInstall
    Only verify and unpack; do not run install.cmd.

.EXAMPLE
    .\gupkg-bootstrap.cmd
    Installs gupkg for the current user.

.EXAMPLE
    .\gupkg-bootstrap.cmd -Scope system -Source C:\drop\gupkg-1.0.0-win-amd64.zip -Sha256 <digest>
    Installs a vetted release for all users (run from an elevated shell).
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'user', 'system')]
    [string]$Scope = 'auto',
    [string]$Source,
    [string]$Sha256,
    [string]$Root,
    [string]$Stage,
    [switch]$SkipInstall
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
    # Windows PowerShell 5.1's Expand-Archive does not guard '..' entries, so every entry is checked to stay below the
    # destination before extracting. Extracting this way writes no Mark of the Web on the files.
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

function Get-AdminOnlyProblem([string]$Path) {
    # Returns why $Path can be changed by someone other than Administrators, SYSTEM or TrustedInstaller, or $null when it
    # cannot. The owner must be one of them, and no other security identifier may hold a write-type right.
    $trusted = @('S-1-5-32-544', 'S-1-5-18', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return 'it is a link' }
    $acl = Get-Acl -LiteralPath $Path
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($trusted -notcontains $owner) { return "its owner is $owner, not Administrators" }
    $FS = [Security.AccessControl.FileSystemRights]
    $write = $FS::WriteData -bor $FS::AppendData -bor $FS::WriteExtendedAttributes -bor $FS::WriteAttributes -bor
        $FS::DeleteSubdirectoriesAndFiles -bor $FS::Delete -bor $FS::ChangePermissions -bor $FS::TakeOwnership
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if (($rule.FileSystemRights -band $write) -eq 0) { continue }
        $sid = $rule.IdentityReference.Value
        if ($trusted -contains $sid) { continue }
        # An inherit-only CREATOR OWNER entry gives rights only to whoever creates a child, never to a user here.
        if ($sid -eq 'S-1-3-0' -and ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }
        return "$sid can write to it"
    }
    return $null
}

function New-SystemRoot([string]$Path) {
    # Creates the system root with its owner and permissions in the same call, so nobody can write to it in between:
    # owned by Administrators, full control for Administrators and SYSTEM, read and execute for Users.
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetSecurityDescriptorSddlForm('O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)')
    [void][IO.Directory]::CreateDirectory($Path, $security)
    $problem = Get-AdminOnlyProblem $Path
    if ($problem) { throw "Could not make $Path admin-only: $problem." }
}

function Find-VersionDirectory([string]$Folder) {
    # A distribution holds <name>\<version>\ with pkg.toml and install.cmd; choose the shallowest, and refuse an ambiguous one.
    $candidates = @(Get-ChildItem -LiteralPath $Folder -Recurse -Directory -Force) + @(Get-Item -LiteralPath $Folder)
    $found = @($candidates |
        Where-Object { (Test-Path (Join-Path $_.FullName 'pkg.toml')) -and (Test-Path (Join-Path $_.FullName 'install.cmd')) })
    if ($found.Count -eq 0) { throw "No version directory (a folder with pkg.toml and install.cmd) was found in: $Source" }
    $depth = ($found | ForEach-Object { $_.FullName.Length } | Measure-Object -Minimum).Minimum
    $shallow = @($found | Where-Object { $_.FullName.Length -eq $depth })
    if ($shallow.Count -ne 1) { throw "The distribution holds more than one version directory: $(($shallow | ForEach-Object { $_.FullName }) -join ', ')" }
    return $shallow[0].FullName
}

# Dot-sourcing this file only defines the functions above (the tests do that); running it installs.
if ($MyInvocation.InvocationName -eq '.') { return }

$work = Join-Path ([IO.Path]::GetTempPath()) ("gupkg-bootstrap-" + [Guid]::NewGuid().ToString('N'))
try {
    # 1. Decide the scope, and refuse early when it cannot work.
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
    if ($Scope -eq 'user' -and $isAdmin) {
        throw "User scope must run from a shell that is not elevated: gupkg never acts on a user's folders as administrator. Open a normal PowerShell, or use -Scope system."
    }
    if (-not $Root) { $Root = Join-Path ($env:SystemDrive + '\') 'opt' }
    $scopeNote = if ($Scope -eq 'system') { "all users; system root $Root" } else { 'current user only' }
    Step "Install scope: $Scope ($scopeNote; $why)"
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    if (-not $Source) {
        $latest = Get-LatestRelease
        $Source = $latest.Url
        $Sha256 = $latest.Sha256
    }

    # 2. Get the archive (or folder) and verify it before unpacking anything.
    $archive = $null
    if (Test-Path -LiteralPath $Source -PathType Container) {
        if ($Scope -eq 'system') { throw "System scope needs a release ZIP, not a folder: it is unpacked straight into the admin-only root." }
        Step "Using folder $Source"
        $tree = $Source
    } else {
        $archive = Join-Path $work 'distribution.zip'
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
    }

    # 3. Unpack. User scope: into a staging folder. System scope: straight into the admin-only system root, which is
    #    created safely or refused first.
    if ($archive -and $Scope -eq 'system') {
        if (Test-Path -LiteralPath $Root) {
            $problem = Get-AdminOnlyProblem $Root
            if ($problem) { throw "$Root already exists and is not admin-only ($problem). Remove it or ask the administrator who made it; refusing to install into it." }
            Step "Using the existing admin-only root $Root"
        } else {
            Step "Creating the admin-only root $Root"
            New-SystemRoot $Root
        }
        Expand-ZipSafely $archive $Root
        $tree = $Root
        $versionDir = Find-VersionDirectory $Root
    } elseif ($archive) {
        $tree = if ($Stage) { $Stage } else { Join-Path $work 'unpacked' }
        Expand-ZipSafely $archive $tree
        $versionDir = Find-VersionDirectory $tree
    } else {
        $versionDir = Find-VersionDirectory $tree
    }
    Step "Version directory: $versionDir"
    if ($SkipInstall) {
        Step "Verified and unpacked. Finish later with: `"$versionDir\install.cmd`" --mode $Scope"
        return
    }

    # 4. Run its install.cmd: it finds the gupkg that the distribution carries (a system root's forwarder, when there is one,
    #    otherwise the bundled copy) and installs the package. The elevated launcher verifies everything it runs first.
    Step 'Running install.cmd'
    $env:GUPKG_NO_PAUSE = '1'
    # Called directly: wrapping it in `cmd /c "<path>"` breaks when the path has an ampersand (cmd strips the quotes).
    & "$versionDir\install.cmd" --mode $Scope
    if ($LASTEXITCODE -ne 0) { throw "install.cmd failed (exit $LASTEXITCODE)." }
    Step 'Done. Open a NEW terminal, then try:  gupkg --help'
    if ($Scope -eq 'system') { Step "The root forwarder is $Root\.gupkg\gupkg.cmd; package launchers of this root now find the installed gupkg through it." }
}
catch {
    [Console]::Error.WriteLine("[gupkg] ERROR: $($_.Exception.Message)")
    exit 1
}
finally {
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}
