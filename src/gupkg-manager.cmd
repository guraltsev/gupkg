@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem gupkg-manager of this tree: the general manager program (the `gupkg manager` command, the gupkg-manager shim and the
rem root forwarder's gupkg-manager.cmd start it). It starts only the program beside this file; nothing is searched.

set "GUPKG_NATIVE=%~dp0gupkg\gupkg-manager.exe"
if not exist "%GUPKG_NATIVE%" goto :script
if exist "%GUPKG_NATIVE%\NUL" goto :script
"%GUPKG_NATIVE%" %*
exit /b %ERRORLEVEL%

:script
set "GUPKG_TARGET=%~dp0gupkg\gupkg-manager.cmd"
if not exist "%GUPKG_TARGET%" goto :missing
if exist "%GUPKG_TARGET%\NUL" goto :missing
call "%GUPKG_TARGET%" %*
exit /b %ERRORLEVEL%

:missing
>&2 echo [gupkg] This gupkg is incomplete: %GUPKG_TARGET% is missing.
exit /b 9009
