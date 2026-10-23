@echo off
rem Build the self-contained release ZIP.   scripts\build-standalone.cmd [output.zip]
rem Fetches the pinned embeddable Python once (SHA-256 verified, kept in dist\cache), installs the
rem hash-locked dependencies into it, writes MANIFEST.sha256, and builds
rem dist\gupkg-<version>-win-amd64.zip unless an output path is given. The ZIP's SHA-256 is printed.
setlocal EnableExtensions
rem The build machine's pip (the development .venv) installs the wheels; the bundle itself has no pip.
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"

set "OUTPUT=%~1"
if defined OUTPUT (set "OUTPUT_ARGUMENT=--output "%OUTPUT%"") else (set "OUTPUT_ARGUMENT=")
%PY% tools\build_standalone.py %OUTPUT_ARGUMENT% || (popd & exit /b 1)
echo [build] Done. The bundle is in dist\ unless you gave an output path.
popd
exit /b 0
