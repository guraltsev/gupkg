@echo off
rem Test, build, publish and record a gupkg release.   scripts\release.cmd ["one-line notes"] [--no-push]
rem Run scripts\bump.cmd first and commit your changes. Without notes the row says "gupkg <version>".
rem This script:
rem   1. runs the test suite,
rem   2. builds gupkg-<version>-win-amd64.zip and tags the released commit; unless --no-push it then
rem      pushes the tag, uploads the ZIP and its .sha256 file as assets of GitHub release v<version>,
rem      downloads the ZIP again and requires the same SHA-256, and adds the row (with that SHA-256)
rem      to RELEASES.md (tools\release.py; needs the GitHub CLI, gh, signed in),
rem   3. pushes the branch to the first Git remote (skipped with --no-push, which also skips the upload).
rem Afterwards run scripts\check-release.cmd to confirm what the world can download.
setlocal EnableExtensions
set "NOTES="
set "NOPUSH="
for %%A in (%*) do (
    if /I "%%~A"=="--no-push" (set "NOPUSH=1") else if not defined NOTES set "NOTES=%%~A"
)
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"
if not defined NOTES (
    for /f "delims=" %%V in ('%PY% tools\bump_version.py --print') do set "NOTES=gupkg %%V"
)

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
echo [release] Published the release and pushed HEAD to %REMOTE%. Confirm it with: scripts\check-release.cmd
popd
exit /b 0
