# Spike S1 (job 07) harness. Throwaway.
#
# Builds the test launchers and fixtures in -WorkDir (outside the working
# tree), runs every launcher variant against every directory case, and
# prints one result line per combination.
#
# Usage (standard user, then again from an elevated shell):
#   powershell -NoProfile -File run-s1.ps1 -WorkDir <empty temp dir>
#
# It only reads the ACLs of C:\opt, C:\Windows and C:\ProgramData; it never
# writes there. Fixtures it creates under -WorkDir and %USERPROFILE%\Downloads
# are removed at the end.

param([Parameter(Mandatory = $true)][string]$WorkDir)

$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# --- launchers -------------------------------------------------------------
$check = Get-Content -Raw -LiteralPath (Join-Path $here 'acl-check.ps1')
$template = Get-Content -Raw -LiteralPath (Join-Path $here 'launcher-template.cmd')

function New-Launcher([string]$name, [string]$prefix) {
    $text = $template.Replace('@PSPREFIX@', $prefix).Replace('@PSCHECK@', "#S1-PS-BEGIN`n$check")
    $text = $text.Replace("`r`n", "`n").Replace("`n", "`r`n")
    $path = Join-Path $WorkDir $name
    [IO.File]::WriteAllText($path, $text, [Text.Encoding]::ASCII)
    $path
}

# Constrained Language Mode, entered before the check is compiled. This is
# what a WDAC/AppLocker-enforced machine does to -Command input.
$launchers = [ordered]@{
    'full'    = New-Launcher 'launch-full.cmd' ''
    'clm'     = New-Launcher 'launch-clm.cmd' "`$ExecutionContext.SessionState.LanguageMode='ConstrainedLanguage';"
}

# --- fixtures --------------------------------------------------------------
$fx = Join-Path $WorkDir 'fixtures'
New-Item -ItemType Directory -Force -Path $fx | Out-Null

# A user-created ("squatted") C:\opt, with the ACL a new folder in C:\ gets on
# a default Windows install (Authenticated Users: Modify, inherited).
$squat = Join-Path $fx 'squatted-opt'
New-Item -ItemType Directory -Force -Path "$squat\myapp\1.0" | Out-Null
$acl = Get-Acl -LiteralPath $squat
$acl.SetSecurityDescriptorSddlForm('D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1301bf;;;AU)(A;OICI;0x1200a9;;;BU)')
Set-Acl -LiteralPath $squat -AclObject $acl

# A version directory where a standard user was granted write access
# (owned by the current user, plus an explicit Users:Modify entry).
$granted = Join-Path $fx 'granted\myapp\1.0'
New-Item -ItemType Directory -Force -Path $granted | Out-Null
icacls $granted /grant '*S-1-5-32-545:(OI)(CI)M' | Out-Null

# A null DACL (everyone full control).
$nulldacl = Join-Path $fx 'null-dacl'
New-Item -ItemType Directory -Force -Path $nulldacl | Out-Null
$acl = Get-Acl -LiteralPath $nulldacl
$acl.SetSecurityDescriptorSddlForm('D:NO_ACCESS_CONTROL')
Set-Acl -LiteralPath $nulldacl -AclObject $acl

# A junction pointing at the admin-only C:\opt.
$junction = Join-Path $fx 'junction-to-opt'
cmd /c mklink /J "$junction" C:\opt | Out-Null

# A distribution unzipped into Downloads.
$downloads = Join-Path $env:USERPROFILE 'Downloads\gupkg-s1-spike'
New-Item -ItemType Directory -Force -Path "$downloads\myapp\1.0" | Out-Null

$gupkgVersion = Get-ChildItem -LiteralPath 'C:\opt\gupkg' -Directory |
    Where-Object { $_.Name -match '^\d' } | Select-Object -First 1 -ExpandProperty FullName

$cases = [ordered]@{
    '1 admin-only C:\opt + its gupkg version dir'     = @('C:\opt', $gupkgVersion)
    '2 admin-only root, user-writable version dir'    = @('C:\opt', $granted)
    '2b admin-owned dir with Users write (ProgramData)' = @('C:\opt', 'C:\ProgramData')
    '2c admin-only C:\Windows, user-writable Tasks'   = @('C:\Windows', 'C:\Windows\Tasks')
    '2d admin-only C:\Windows + System32 (sibling Tasks is user-writable)' = @('C:\Windows', 'C:\Windows\System32')
    '3 squatted root (user-created C:\opt)'           = @($squat, "$squat\myapp\1.0")
    '4 Downloads'                                     = @($downloads, "$downloads\myapp\1.0")
    '5 null DACL'                                     = @('C:\opt', $nulldacl)
    '6 junction to C:\opt as root'                    = @($junction, $gupkgVersion)
    '7 path with ..'                                  = @("$gupkgVersion\..\..", $gupkgVersion)
    '8 missing directory'                             = @('C:\opt', 'C:\opt\does-not-exist-s1')
}

function Invoke-Launcher([string]$launcher, [string]$root, [string]$ver, [hashtable]$extraEnv) {
    $env:S1_TEST_ROOT = $root
    $env:S1_TEST_VERSIONDIR = $ver
    foreach ($k in $extraEnv.Keys) { Set-Item "env:$k" $extraEnv[$k] }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $out = & cmd.exe /d /c "`"$launcher`"" 2>&1
    $code = $LASTEXITCODE
    $sw.Stop()
    foreach ($k in $extraEnv.Keys) { Remove-Item "env:$k" }
    [pscustomobject]@{ Code = $code; Ms = $sw.ElapsedMilliseconds; Out = ($out -join ' | ') }
}

$results = @()
$variants = [ordered]@{
    'full'          = @{ L = $launchers.full; E = @{} }
    'clm'           = @{ L = $launchers.clm; E = @{} }
    'clm-envvar'    = @{ L = $launchers.full; E = @{ '__PSLockdownPolicy' = '4' } }
    'ps-missing'    = @{ L = $launchers.full; E = @{ 'S1_TEST_PSEXE' = 'C:\no-such-dir\powershell.exe' } }
    'ps-wrong-exe'  = @{ L = $launchers.full; E = @{ 'S1_TEST_PSEXE' = "$env:SystemRoot\System32\whoami.exe" } }
}
foreach ($v in $variants.Keys) {
    foreach ($c in $cases.Keys) {
        $r = Invoke-Launcher $variants[$v].L $cases[$c][0] $cases[$c][1] $variants[$v].E
        $results += [pscustomobject]@{ Variant = $v; Case = $c; Exit = $r.Code; Ms = $r.Ms; Output = $r.Out }
    }
}

# Which language mode does each variant really get?
$probe = '$ExecutionContext.SessionState.LanguageMode'
$ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$modeFull = & $ps -NoProfile -NonInteractive -Command $probe
$env:__PSLockdownPolicy = '4'
$modeEnv = & $ps -NoProfile -NonInteractive -Command $probe
Remove-Item env:__PSLockdownPolicy
$clmProbe = "`$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'; Invoke-Expression '`$ExecutionContext.SessionState.LanguageMode; try { [System.IO.File]::Exists(''C:\'') } catch { ''method call blocked'' }'"
$modeClm = & $ps -NoProfile -NonInteractive -Command $clmProbe

# Get-Acl on a junction: whose ACL comes back?
$jSddl = (Get-Acl -LiteralPath $junction).Sddl
$tSddl = (Get-Acl -LiteralPath 'C:\opt').Sddl

# Cost of a recursive check over an installed gupkg tree.
$sw = [Diagnostics.Stopwatch]::StartNew()
$saveFile = Join-Path $WorkDir 'icacls-save.txt'
icacls 'C:\opt\gupkg' /save $saveFile /T /C /Q | Out-Null
$sw.Stop()
$icaclsMs = $sw.ElapsedMilliseconds
$icaclsEntries = (Get-Content -LiteralPath $saveFile -Encoding Unicode | Measure-Object).Count / 2
$fileCount = (Get-ChildItem -LiteralPath 'C:\opt\gupkg' -Recurse -Force | Measure-Object).Count

# --- report ----------------------------------------------------------------
"windows: $((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').DisplayVersion) build $([Environment]::OSVersion.Version.Build).$((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').UBR)"
"powershell: $($PSVersionTable.PSVersion)"
"whoami /groups integrity: $((whoami /groups | Select-String 'Mandatory Level').ToString().Trim())"
"language mode, plain -Command: $modeFull"
"language mode, __PSLockdownPolicy=4 in process env: $modeEnv"
"language mode, CLM set in session: $($modeClm -join ' / ')"
"Get-Acl on junction == Get-Acl on target: $($jSddl -eq $tSddl)"
"recursive icacls /save over C:\opt\gupkg: $fileCount items, $icaclsEntries ACL entries, $icaclsMs ms"
""
$results | Format-Table -AutoSize -Wrap | Out-String -Width 400

# --- cleanup ---------------------------------------------------------------
cmd /c rmdir "$junction" | Out-Null
Remove-Item -LiteralPath $downloads -Recurse -Force
