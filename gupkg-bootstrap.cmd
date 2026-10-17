@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Launches gupkg-bootstrap.ps1 with the same arguments, bypassing the
rem script execution policy for this one run only. When the script is not beside
rem this file, offers to download it from the official repository first.
rem   GUPKG_BOOTSTRAP_YES=1  download without asking
rem   GUPKG_BOOTSTRAP_URL    download from another address (a mirror or a fork)

set "GUPKG_PS1=%~dp0gupkg-bootstrap.ps1"
set "GUPKG_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%GUPKG_PS1%" goto :run

if not defined GUPKG_BOOTSTRAP_URL set "GUPKG_BOOTSTRAP_URL=https://raw.githubusercontent.com/guraltsev/gupkg/main/gupkg-bootstrap.ps1"
echo [gupkg] gupkg-bootstrap.ps1 was not found next to this file.
if defined GUPKG_BOOTSTRAP_YES goto :download
echo [gupkg] It can be downloaded from:
echo         %GUPKG_BOOTSTRAP_URL%
set "GUPKG_ANSWER="
set /p "GUPKG_ANSWER=[gupkg] Download it to %~dp0 and continue? [Y/n] "
if /I "%GUPKG_ANSWER%"=="n" goto :declined
if /I "%GUPKG_ANSWER%"=="no" goto :declined

:download
rem Fetch to a temporary name and rename only a complete, non-empty file.
"%GUPKG_POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; $part=$env:GUPKG_PS1+'.download'; Invoke-WebRequest -UseBasicParsing -Uri $env:GUPKG_BOOTSTRAP_URL -OutFile $part; if ((Get-Item -LiteralPath $part).Length -eq 0) { throw 'The downloaded file is empty.' }; Move-Item -LiteralPath $part -Destination $env:GUPKG_PS1 -Force"
if errorlevel 1 (
    >&2 echo [gupkg] Could not download gupkg-bootstrap.ps1.
    del "%GUPKG_PS1%.download" >nul 2>nul
    exit /b 1
)
echo [gupkg] Saved %GUPKG_PS1%

:run
"%GUPKG_POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -File "%GUPKG_PS1%" %*
exit /b %ERRORLEVEL%

:declined
>&2 echo [gupkg] Cancelled; gupkg-bootstrap.ps1 is required.
exit /b 1
