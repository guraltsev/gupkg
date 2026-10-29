@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem gupkg-manager of this tree: the general manager program (the `gupkg manager` command, and the gupkg-manager shim
rem start it). It starts only the program beside this file; nothing is searched.

set "GUPKG_TARGET=%~dp0gupkg\gupkg-manager.cmd"
if not exist "%GUPKG_TARGET%" goto :missing
if exist "%GUPKG_TARGET%\NUL" goto :missing
call "%GUPKG_TARGET%" %*
exit /b %ERRORLEVEL%

:missing
>&2 echo [gupkg] This gupkg is incomplete: %GUPKG_TARGET% is missing.
exit /b 9009
