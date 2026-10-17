@echo off
rem Copy src\gupkg.cmd into every package in pkgs\.   scripts\sync-package-launchers.cmd [--check]
rem With --check, only report packages whose copy is missing or different (exit status 1).
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"
%PY% tools\sync_package_launchers.py %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
