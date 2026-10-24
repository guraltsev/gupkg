@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem gupkg-tui of this tree: the text interface program (the `gupkg tui` command and the gupkg-tui shim start it).
rem It starts only the program beside this file (gupkg\gupkg-tui.cmd); nothing is searched.

set "GUPKG_TARGET=%~dp0gupkg\gupkg-tui.cmd"
if not exist "%GUPKG_TARGET%" goto :missing
if exist "%GUPKG_TARGET%\NUL" goto :missing
call "%GUPKG_TARGET%" %*
exit /b %ERRORLEVEL%

:missing
>&2 echo [gupkg] This gupkg is incomplete: %GUPKG_TARGET% is missing.
exit /b 9009
