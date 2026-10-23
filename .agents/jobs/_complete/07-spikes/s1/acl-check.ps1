# Spike S1 (job 07): the fixed ACL check run by elevated package launchers.
#
# Throwaway spike code, not product code. The launcher carries this script
# after a marker line and runs it with
# %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe, so no
# separate checker file lives in the checked location. (-EncodedCommand was
# tried first: the encoded script exceeds cmd's 8191-character line limit.)
#
# Input:  $env:GUPKG_ACL_PATHS, directories separated by '|'.
# Output: exactly one line "GUPKG-ACL-OK" when every directory passes;
#         otherwise one "GUPKG-ACL-FAIL <path>: <reason>" line per problem.
#
# The script is written for Constrained Language Mode: it uses only cmdlets,
# operators, hashtables and the .Sddl property. It invokes no .NET methods and
# names no .NET types beyond [int64]. Security identifiers are compared as
# SDDL strings, so localized account names never matter.

$ErrorActionPreference = 'Stop'

$ti = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
$trusted = @{ 'S-1-5-32-544' = 1; 'S-1-5-18' = 1; $ti = 1 }
$alias = @{ 'BA' = 'S-1-5-32-544'; 'SY' = 'S-1-5-18'; 'CO' = 'S-1-3-0' }

# SDDL right tokens -> access mask bits (file and directory meaning).
$tokens = @{
    'GA' = 0x10000000; 'GX' = 0x20000000; 'GW' = 0x40000000; 'GR' = [int64]2147483648
    'RC' = 0x20000; 'SD' = 0x10000; 'WD' = 0x40000; 'WO' = 0x80000
    'CC' = 0x1; 'DC' = 0x2; 'LC' = 0x4; 'SW' = 0x8; 'RP' = 0x10; 'WP' = 0x20
    'DT' = 0x40; 'LO' = 0x80; 'CR' = 0x100
    'FA' = 0x1F01FF; 'FR' = 0x120089; 'FW' = 0x120116; 'FX' = 0x1200A0
}
# write data/add file, append/add subdirectory, write EA, delete child,
# write attributes, delete, write DAC, write owner, GENERIC_ALL, GENERIC_WRITE,
# MAXIMUM_ALLOWED.
$writeMask = [int64](0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000 -bor 0x02000000)

$failures = @()
$paths = @($env:GUPKG_ACL_PATHS -split '\|' | Where-Object { $_ -ne '' })
if ($paths.Count -eq 0) { $failures += 'no paths given' }

foreach ($path in $paths) {
    try {
        $item = Get-Item -LiteralPath $path -Force
        if (-not $item.PSIsContainer) { $failures += "${path}: not a directory"; continue }
        if ($item.Attributes -match 'ReparsePoint') { $failures += "${path}: is a reparse point"; continue }
        $sddl = (Get-Acl -LiteralPath $path).Sddl
    } catch {
        $failures += "${path}: cannot read ACL"
        continue
    }
    # Get-Acl renders a null DACL (everyone has full access) as no D: part.
    if ($sddl -notmatch 'D:') { $failures += "${path}: null DACL (everyone has full access)"; continue }
    if ($sddl -notmatch '^O:(?<o>.+?)(G:(?<g>.+?))?D:(?<d>.*)$') {
        $failures += "${path}: unparsable SDDL"
        continue
    }
    $owner = $Matches['o']
    $dacl = $Matches['d']
    if ($alias.ContainsKey($owner)) { $owner = $alias[$owner] }
    if (-not $trusted.ContainsKey($owner)) { $failures += "${path}: owner $owner is not Administrators, SYSTEM or TrustedInstaller" }

    if ($dacl -match 'NO_ACCESS_CONTROL') { $failures += "${path}: null DACL (everyone has full access)"; continue }
    if ($dacl -notmatch '^(?<flags>[A-Z]*)(?<aces>(\([^()]*\))*)$') { $failures += "${path}: unparsable DACL"; continue }
    $aceText = $Matches['aces']
    $aces = @()
    if ($aceText -ne '') { $aces = @($aceText.Substring(1, $aceText.Length - 2) -split '\)\(') }
    foreach ($ace in $aces) {
        $f = $ace -split ';'
        if ($f.Count -lt 6) { $failures += "${path}: unparsable ACE ($ace)"; continue }
        $type = $f[0]; $aceFlags = $f[1]; $rights = $f[2]; $sid = $f[5]
        if ($type -eq 'D' -or $type -eq 'OD') { continue }
        if ($type -ne 'A' -and $type -ne 'OA') { $failures += "${path}: unsupported ACE type ($ace)"; continue }
        if ($alias.ContainsKey($sid)) { $sid = $alias[$sid] }
        $mask = [int64]0
        if ($rights -match '^0x[0-9A-Fa-f]+$') {
            $mask = [int64]$rights
        } elseif ($rights -match '^([A-Z]{2})+$') {
            for ($i = 0; $i -lt $rights.Length; $i += 2) {
                $t = $rights.Substring($i, 2)
                if (-not $tokens.ContainsKey($t)) { $mask = $writeMask; break }
                $mask = $mask -bor [int64]$tokens[$t]
            }
        } else {
            $failures += "${path}: unparsable rights ($ace)"; continue
        }
        if (($mask -band $writeMask) -eq 0) { continue }
        if ($trusted.ContainsKey($sid)) { continue }
        # CREATOR OWNER only gives rights to whoever creates a child. Only
        # trusted principals can create children here, so an inherit-only
        # CREATOR OWNER entry is safe.
        if ($sid -eq 'S-1-3-0' -and $aceFlags -match 'IO') { continue }
        $failures += "${path}: $sid has write-type rights ($ace)"
    }
}

if ($failures.Count -eq 0) {
    'GUPKG-ACL-OK'
    exit 0
}
$failures | ForEach-Object { "GUPKG-ACL-FAIL $_" }
exit 1
