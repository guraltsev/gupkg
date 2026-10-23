@echo off
rem Provision a Python runtime from the pinned embeddable CPython.   scripts\fetch-runtime.cmd [tools\fetch_runtime.py options]
rem Downloads and verifies the pinned archive (dist\cache), writes ._pth and installs the hash-locked
rem dependencies (tools\gupkg-deps.lock). The default destination is the committed runtime,
rem src\gupkg\python; an existing runtime is replaced only with --force.
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
rem Installing the lock needs pip, which the committed runtime provides (a plain venv may not).
if exist "%REPO_ROOT%\src\gupkg\python\python.exe" set "PY="%REPO_ROOT%\src\gupkg\python\python.exe""
pushd "%REPO_ROOT%"
%PY% tools\fetch_runtime.py %* || (popd & exit /b 1)
popd
exit /b 0
