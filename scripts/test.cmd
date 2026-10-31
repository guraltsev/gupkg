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
rem Run the test suite by tier (_docs/tests.md section 5).   scripts\test.cmd [--process | --all] [pytest arguments]
rem   (no option)  the unit tier: nothing but the process itself. Run it after every edit.
rem   --process    the unit and process tiers (real child processes, links, loopback). Run it before saying a change is done.
rem   --all        every tier, release included. Run it before a merge or a commit to main, and after a change to the build.
rem Every other argument goes to pytest, for example: scripts\test.cmd tests\catalog -x
rem The last line printed names the tiers that ran.
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
%PY% -c "import pytest" >nul 2>nul
if errorlevel 1 (
    >&2 echo [scripts] pytest is not installed in .venv. Run: .venv\Scripts\pip install -r requirements-dev.txt
    exit /b 1
)
set "TIERS=unit"
set "TIER_ARGS=-m unit"
if /i "%~1"=="--process" (
    set "TIERS=unit, process"
    set "TIER_ARGS=-m "unit or process""
    shift
) else if /i "%~1"=="--all" (
    set "TIERS=unit, process, release"
    set "TIER_ARGS="
    shift
)
set "PYTEST_ARGS="
:collect
if "%~1"=="" goto :run
set PYTEST_ARGS=%PYTEST_ARGS% %1
shift
goto :collect
:run
pushd "%REPO_ROOT%"
%PY% -m pytest -q %TIER_ARGS% %PYTEST_ARGS%
set "RESULT=%ERRORLEVEL%"
popd
echo [scripts] Tiers run: %TIERS%
exit /b %RESULT%
