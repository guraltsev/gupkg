@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem gupkg-manager of this tree: the general manager program (the `gupkg manager` command, and the gupkg-manager shim
rem start it). It starts only the program beside this file; nothing is searched.

set "GUPKG_TARGET=%~dp0gupkg\gupkg-manager.cmd"
if not exist "%GUPKG_TARGET%" goto :missing
if exist "%GUPKG_TARGET%\NUL" goto :missing
rem No `call`: call would parse the arguments a second time (it expands %%NAME%% again and doubles every caret), so the
rem inner launcher would not receive what the caller passed. The inner launcher ends with exit /b, which is this one's result.
"%GUPKG_TARGET%" %*
exit /b %ERRORLEVEL%

:missing
>&2 echo [gupkg] This gupkg is incomplete: %GUPKG_TARGET% is missing.
>&2 echo [gupkg] This file only starts the program in the gupkg folder beside it, so it works only inside a complete gupkg folder.
>&2 echo [gupkg] To open or install a package, use manage.cmd or install.cmd from its version folder (gupkg pkg launchers VERSION-DIR writes them).
exit /b 9009
