@echo off
rem Test, build, publish and record a gupkg release.   scripts\release.cmd "one-line notes" [--no-push]
rem Run scripts\bump.cmd first and commit your changes. This script:
rem   1. runs the test suite,
rem   2. builds gupkg-<version>-win-amd64.zip, tags the released commit, uploads the ZIP as the
rem      asset of GitHub release v<version> and adds the row (with the ZIP's SHA-256) to RELEASES.md
rem      (tools\release.py; needs the GitHub CLI, gh, signed in),
rem   3. pushes the branch to the first Git remote (skipped with --no-push, which also skips the upload).
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
if defined NOPUSH (
    %PY% tools\release.py "%NOTES%" --no-publish || (popd & exit /b 1)
    echo [release] Not publishing. Upload the ZIP and push yourself: git push ^<remote^> HEAD and the new v^<version^> tag.
    popd & exit /b 0
)
%PY% tools\release.py "%NOTES%" || (popd & exit /b 1)

rem The release row is committed after the tag was pushed; push the branch with it.
set "REMOTE="
for /f "delims=" %%R in ('git remote') do if not defined REMOTE set "REMOTE=%%R"
if not defined REMOTE (>&2 echo [release] No Git remote is configured. & popd & exit /b 1)
git push %REMOTE% HEAD || (popd & exit /b 1)
echo [release] Published the release and pushed HEAD to %REMOTE%.
popd
exit /b 0
