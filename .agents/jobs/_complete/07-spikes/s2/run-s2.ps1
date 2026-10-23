# Spike S2 (job 07): Mark of the Web on a distribution ZIP. Throwaway.
#
# Builds myapp-1.0.zip (myapp\1.0\{install.cmd, gupkg\python\..., App\dummy.txt}),
# gives it the Zone.Identifier stream Edge writes for an internet download,
# extracts it with Explorer's ZIP handler (Shell.Application CopyHere) and
# with tar -xf, with and without Unblock-File before extraction, and records:
#   - which extracted files carry Zone.Identifier;
#   - whether install.cmd runs bundled python.exe when started from cmd.exe
#     (how install.cmd chains to gupkg) and when started through ShellExecute
#     (a double-click; detected by a marker file within a timeout).
#
# Usage: powershell -NoProfile -File run-s2.ps1 -WorkDir <empty temp dir> [-ShellExecute]

param(
    [Parameter(Mandatory = $true)][string]$WorkDir,
    [switch]$ShellExecute
)

$ErrorActionPreference = 'Continue'
$repo = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '..\..\..\..\..')
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# --- build the distribution -------------------------------------------------
$src = Join-Path $WorkDir 'build\myapp\1.0'
New-Item -ItemType Directory -Force -Path "$src\App", "$src\gupkg" | Out-Null
Copy-Item -Recurse -Path (Join-Path $repo 'src\gupkg\python') -Destination "$src\gupkg\python"
Set-Content -LiteralPath "$src\App\dummy.txt" -Value 'dummy App payload' -Encoding ascii
$install = @'
@echo off
"%~dp0gupkg\python\python.exe" -I -s -B -c "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('python ran ' + sys.version.split()[0])" "%~dp0marker.txt"
exit /b %errorlevel%
'@
[IO.File]::WriteAllText("$src\install.cmd", $install.Replace("`r`n", "`n").Replace("`n", "`r`n"), [Text.Encoding]::ASCII)

$zip = Join-Path $WorkDir 'myapp-1.0.zip'
Compress-Archive -Path (Join-Path $WorkDir 'build\myapp') -DestinationPath $zip -Force

$motw = "[ZoneTransfer]`r`nZoneId=3`r`nReferrerUrl=https://example.invalid/`r`nHostUrl=https://example.invalid/myapp-1.0.zip`r`n"

function Set-Motw([string]$path) { Set-Content -LiteralPath $path -Stream Zone.Identifier -Value $motw -Encoding ascii }

function Test-Motw([string]$path) {
    $null -ne (Get-Item -LiteralPath $path -Stream Zone.Identifier -ErrorAction SilentlyContinue)
}

function Expand-Explorer([string]$zipPath, [string]$dest) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $shell = New-Object -ComObject Shell.Application
    # 4 = no progress dialog, 16 = yes to all, 1024 = no error UI
    $shell.NameSpace($dest).CopyHere($shell.NameSpace($zipPath).Items(), 4 + 16 + 1024)
    $deadline = (Get-Date).AddMinutes(5)
    while (-not (Test-Path "$dest\myapp\1.0\install.cmd") -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    # CopyHere is asynchronous: wait until the file count stops changing.
    $last = -1
    while ($true) {
        $n = (Get-ChildItem -LiteralPath $dest -Recurse -File | Measure-Object).Count
        if ($n -eq $last) { break }
        $last = $n
        Start-Sleep -Seconds 2
    }
}

function Expand-Tar([string]$zipPath, [string]$dest) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    & "$env:SystemRoot\System32\tar.exe" -xf $zipPath -C $dest
}

function Invoke-ViaCmd([string]$dir) {
    Remove-Item -LiteralPath "$dir\marker.txt" -ErrorAction SilentlyContinue
    $out = & cmd.exe /d /c "`"$dir\install.cmd`"" 2>&1
    $code = $LASTEXITCODE
    $marker = if (Test-Path "$dir\marker.txt") { Get-Content -LiteralPath "$dir\marker.txt" } else { '<none>' }
    "exit=$code marker=$marker out=$($out -join ' ')"
}

function Invoke-ViaShell([string]$dir) {
    Remove-Item -LiteralPath "$dir\marker.txt" -ErrorAction SilentlyContinue
    $job = Start-Job -ScriptBlock { param($p) Start-Process -FilePath $p -Wait } -ArgumentList "$dir\install.cmd"
    $deadline = (Get-Date).AddSeconds(20)
    while (-not (Test-Path "$dir\marker.txt") -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    $ran = Test-Path "$dir\marker.txt"
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    if ($ran) { "ran (marker written)" } else { "did not run within 20 s (blocked or waiting on a prompt)" }
}

$results = @()
foreach ($unblock in @($false, $true)) {
    $z = Join-Path $WorkDir ("dl-" + ($(if ($unblock) { 'unblocked' } else { 'motw' })) + '.zip')
    Copy-Item -LiteralPath $zip -Destination $z
    Set-Motw $z
    if ($unblock) { Unblock-File -LiteralPath $z }
    foreach ($how in @('explorer', 'tar')) {
        $dest = Join-Path $WorkDir ("x-$how-" + $(if ($unblock) { 'unblocked' } else { 'motw' }))
        if ($how -eq 'explorer') { Expand-Explorer $z $dest } else { Expand-Tar $z $dest }
        $dir = "$dest\myapp\1.0"
        $files = Get-ChildItem -LiteralPath $dest -Recurse -File
        $marked = @($files | Where-Object { Test-Motw $_.FullName }).Count
        $row = [ordered]@{
            Zip           = $(if ($unblock) { 'Unblock-File before extract' } else { 'MotW (ZoneId=3)' })
            Extract       = $how
            Files         = $files.Count
            WithMotw      = $marked
            InstallCmd    = Test-Motw "$dir\install.cmd"
            PythonExe     = Test-Motw "$dir\gupkg\python\python.exe"
            RunFromCmd    = Invoke-ViaCmd $dir
        }
        if ($ShellExecute) { $row.RunViaShellExecute = Invoke-ViaShell $dir }
        $results += [pscustomobject]$row
    }
}

# Unblock-File after extraction, recursively, on the marked Explorer tree.
$fixed = Join-Path $WorkDir 'x-explorer-motw'
Get-ChildItem -LiteralPath $fixed -Recurse -File | Unblock-File
$afterUnblock = @(Get-ChildItem -LiteralPath $fixed -Recurse -File | Where-Object { Test-Motw $_.FullName }).Count

"windows: $((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').DisplayVersion) build $([Environment]::OSVersion.Version.Build).$((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').UBR)"
"smart app control (VerifiedAndReputablePolicyState, 0=off 1=on 2=evaluation): $((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState)"
"zip size MB: $([math]::Round((Get-Item $zip).Length / 1MB, 1))"
"files still marked after recursive Unblock-File on the extracted Explorer tree: $afterUnblock"
$results | Format-List | Out-String -Width 300
