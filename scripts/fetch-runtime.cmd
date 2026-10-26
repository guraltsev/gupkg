@echo off
if defined GUPKG_IN_SCRIPT goto :main
set "GUPKG_IN_SCRIPT=1"
call "%~f0" %*
set "GUPKG_RC=%ERRORLEVEL%"
set "GUPKG_IN_SCRIPT="
rem A script a person starts waits for a key so its window stays readable; GUPKG_NO_PAUSE turns that off (tests, other scripts).
if not defined GUPKG_NO_PAUSE pause
exit /b %GUPKG_RC%
:main
rem Provision a Python runtime from the pinned embeddable CPython.   scripts\fetch-runtime.cmd [tools\fetch_runtime.py options]
rem Downloads and verifies the pinned archive (dist\cache), writes ._pth and installs the hash-locked
rem dependencies (tools\gupkg-deps.lock). The default destination is the committed runtime,
rem src\gupkg\python; an existing runtime is replaced only with --force.
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"
%PY% tools\fetch_runtime.py %* || (popd & exit /b 1)
popd
exit /b 0
