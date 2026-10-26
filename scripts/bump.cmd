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
rem Bump the version and commit it.
rem   scripts\bump.cmd            raise the patch number (1.0.0 -> 1.0.1)
rem   scripts\bump.cmd minor      raise the minor number (1.0.1 -> 1.1.0); major works the same way
rem   scripts\bump.cmd 1.2.3      set exactly this version
rem Updates src\gupkg\_version.py, the single source of the version. Publish with scripts\release.cmd,
rem then confirm the published release with scripts\check-release.cmd.
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"
%PY% tools\bump_version.py %1 || (popd & exit /b 1)
set "NEWVERSION="
for /f "delims=" %%V in ('%PY% tools\bump_version.py --print') do set "NEWVERSION=%%V"
git add src\gupkg\_version.py || (popd & exit /b 1)
git commit -m "Bump version to %NEWVERSION%" || (popd & exit /b 1)
echo [bump] Version is now %NEWVERSION%. Publish it with: scripts\release.cmd
popd
exit /b 0
