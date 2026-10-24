@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem gupkg of this tree: the command that the root forwarder, the shims and the package launchers call.
rem It starts only the gupkg beside this file (gupkg\gupkg.exe, else gupkg\gupkg.cmd). PATH, the registry, AppData,
rem ProgramData and the current directory are never searched (design 21.3).

set "GUPKG_NATIVE=%~dp0gupkg\gupkg.exe"
if not exist "%GUPKG_NATIVE%" goto :script
if exist "%GUPKG_NATIVE%\NUL" goto :script
"%GUPKG_NATIVE%" %*
exit /b %ERRORLEVEL%

:script
set "GUPKG_TARGET=%~dp0gupkg\gupkg.cmd"
if not exist "%GUPKG_TARGET%" goto :missing
if exist "%GUPKG_TARGET%\NUL" goto :missing
call "%GUPKG_TARGET%" %*
exit /b %ERRORLEVEL%

:missing
>&2 echo [gupkg] This gupkg is incomplete: %GUPKG_TARGET% is missing.
exit /b 9009
