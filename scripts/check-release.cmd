@echo off
rem Check that a published release is what RELEASES.md says.   scripts\check-release.cmd [version]
rem Without a version it checks the one in src\gupkg\_version.py. It downloads the ZIP from the address the
rem bootstrap uses (the GitHub release v<version>) and prints PASS or FAIL for each check:
rem   - RELEASES.md has the row (commit and SHA-256), the local and remote tags point at that commit,
rem     and the pushed branch contains this checkout's HEAD;
rem   - the downloaded ZIP and its .sha256 asset have the SHA-256 of the row;
rem   - RELEASE.txt names the version and the commit, and every file of MANIFEST.sha256 matches.
rem The exit status is 1 if any check fails. Nothing is changed, pushed or uploaded.
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"
%PY% tools\check_release.py %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
