@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Opens the gupkg text interface (install, update, repair) for this package, so
rem no command line is needed. Every registry package ships this file as
rem gupkg-tui.cmd; double-click it or run it.
rem gupkg is taken from a gupkg installed beside this file (a distributed local
rem install) and otherwise from gupkg.exe on PATH. The current directory is
rem never searched.

set "GUPKG_HERE=%~dp0"
set "GUPKG_RUN="
if exist "%GUPKG_HERE%gupkg\gupkg.exe" if not exist "%GUPKG_HERE%gupkg\gupkg.exe\NUL" set "GUPKG_RUN=%GUPKG_HERE%gupkg\gupkg.exe"
if not defined GUPKG_RUN if exist "%GUPKG_HERE%gupkg\gupkg.cmd" if not exist "%GUPKG_HERE%gupkg\gupkg.cmd\NUL" set "GUPKG_RUN=%GUPKG_HERE%gupkg\gupkg.cmd"
if defined GUPKG_RUN goto :found
if not defined PATH goto :found
for %%P in ("%PATH:;=" "%") do if not defined GUPKG_RUN if not "%%~P"=="" if exist "%%~P\gupkg.exe" if not exist "%%~P\gupkg.exe\NUL" set "GUPKG_RUN=%%~fP\gupkg.exe"
:found

if not defined GUPKG_RUN (
    >&2 echo [gupkg] gupkg is not installed, so this package cannot be installed from here.
    >&2 echo [gupkg] Install gupkg first: https://github.com/guraltsev/gupkg
    >&2 echo [gupkg] ^(download gupkg-bootstrap.cmd and run it^), then run this file again.
    if not defined GUPKG_NO_PAUSE pause
    exit /b 9009
)

call "%GUPKG_RUN%" tui "%GUPKG_HERE%."
set "GUPKG_EXIT=%ERRORLEVEL%"
if not "%GUPKG_EXIT%"=="0" if not defined GUPKG_NO_PAUSE pause
exit /b %GUPKG_EXIT%
