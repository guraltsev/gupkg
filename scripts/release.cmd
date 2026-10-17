@echo off
rem Test, record, tag and publish a gupkg release.   scripts\release.cmd "one-line notes" [--no-push]
rem Run scripts\bump.cmd first and commit your changes. This script:
rem   1. runs the test suite,
rem   2. adds the row to RELEASES.md and tags the released commit (tools\release.py),
rem   3. pushes the branch and the tag to the first Git remote (skipped with --no-push).
setlocal EnableExtensions
if "%~1"=="" (
    >&2 echo Usage: scripts\release.cmd "one-line notes" [--no-push]
    exit /b 2
)
set "NOTES=%~1"
set "NOPUSH="
if /I "%~2"=="--no-push" set "NOPUSH=1"
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"

call "%~dp0test.cmd" || (>&2 echo [release] Tests failed; nothing was released. & popd & exit /b 1)
%PY% tools\release.py "%NOTES%" || (popd & exit /b 1)
if defined NOPUSH (
    echo [release] Not pushing. Push with: git push ^<remote^> HEAD and the new v^<version^> tag.
    popd & exit /b 0
)

rem The release tag is the newest v* tag; push it with the branch to the first remote.
set "REMOTE="
for /f "delims=" %%R in ('git remote') do if not defined REMOTE set "REMOTE=%%R"
set "TAG="
for /f "delims=" %%T in ('git for-each-ref --sort^=-creatordate --count^=1 --format^=%%^(refname:short^) refs/tags/v*') do set "TAG=%%T"
if not defined REMOTE (>&2 echo [release] No Git remote is configured. & popd & exit /b 1)
if not defined TAG (>&2 echo [release] No release tag was found. & popd & exit /b 1)
git push %REMOTE% HEAD %TAG% || (popd & exit /b 1)
echo [release] Pushed HEAD and %TAG% to %REMOTE%.
popd
exit /b 0
