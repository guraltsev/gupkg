@echo off
rem Spike S1 (job 07): elevated-launcher check, throwaway test version.
rem run-s1.ps1 fills in the two placeholders: the prefix (empty, or a
rem Constrained Language Mode switch) and acl-check.ps1. The check is carried
rem in this file after the #S1-PS-BEGIN marker: cmd never reaches it, and
rem the System32 PowerShell reads it from %~f0.
rem Test hooks (spike only, a real launcher has none):
rem   S1_TEST_ROOT / S1_TEST_VERSIONDIR  check these instead of %~dp0..\.. and %~dp0
rem   S1_TEST_PSEXE                      run this instead of the System32 PowerShell
setlocal EnableExtensions DisableDelayedExpansion

set "ROOTDIR=%~dp0..\.."
set "VERDIR=%~dp0."
if defined S1_TEST_ROOT set "ROOTDIR=%S1_TEST_ROOT%"
if defined S1_TEST_VERSIONDIR set "VERDIR=%S1_TEST_VERSIONDIR%"

set "ELEVATED=no"
"%SystemRoot%\System32\whoami.exe" /groups | "%SystemRoot%\System32\find.exe" "S-1-16-12288" >nul 2>&1 && set "ELEVATED=yes"

set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if defined S1_TEST_PSEXE set "PSEXE=%S1_TEST_PSEXE%"

set "GUPKG_ACL_PATHS=%ROOTDIR%|%VERDIR%"
set "GUPKG_SELF=%~f0"
rem The command travels in an environment variable: parentheses on the
rem for /f line itself would end the for clause.
set "GUPKG_PSCMD=@PSPREFIX@$t=Get-Content -Raw -LiteralPath $env:GUPKG_SELF;$i=$t.LastIndexOf('#S1-PS-BEGIN');Invoke-Expression $t.Substring($i)"
set "ACLLAST="
set "ACLLINES=0"
for /f "usebackq delims=" %%L in (`""%PSEXE%" -NoProfile -NonInteractive -Command "Invoke-Expression $env:GUPKG_PSCMD"" 2^>nul`) do (
    set "ACLLAST=%%L"
    call :count "%%L"
)
set "ACLRESULT="
if "%ACLLINES%"=="1" if "%ACLLAST%"=="GUPKG-ACL-OK" set "ACLRESULT=OK"

echo elevated=%ELEVATED% lines=%ACLLINES%
if "%ACLRESULT%"=="OK" (
    echo RESULT=PROCEED
    exit /b 0
)
echo RESULT=REFUSE
exit /b 1

:count
set /a ACLLINES+=1
echo   check: %~1
exit /b 0

@PSCHECK@
