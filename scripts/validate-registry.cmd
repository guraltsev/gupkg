@echo off
rem Validate the package registry in the working tree and list its packages.
rem   scripts\validate-registry.cmd [path-to-registry]   (default: gupkg-registry)
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"
%PY% tools\validate_registry.py %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
