@echo off
rem Set the gupkg version everywhere and commit it.   scripts\bump.cmd 0.14.0
rem Updates src\gupkg\_version.py, src\pkg.toml and src\gupkg\pkg.toml.
setlocal EnableExtensions
if "%~1"=="" (
    >&2 echo Usage: scripts\bump.cmd ^<new-version^>   e.g. scripts\bump.cmd 0.14.0
    exit /b 2
)
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"
%PY% tools\bump_version.py "%~1" || (popd & exit /b 1)
git add src\gupkg\_version.py src\pkg.toml src\gupkg\pkg.toml || (popd & exit /b 1)
git commit -m "Bump version to %~1" || (popd & exit /b 1)
popd
exit /b 0
