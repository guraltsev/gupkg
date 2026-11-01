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
rem Run the test suite by tier (_docs/tests.md section 5).   _scripts\test.cmd [--process | --all] [pytest arguments]
rem   (no option)  the unit tier: nothing but the process itself. Required when a task is complete; while working, pass a path to run only the area you are changing, as an aid.
rem   --process    the unit and process tiers (real child processes, links, loopback). Run it before saying a change is done.
rem   --all        every tier, release included. Run it before a merge or a commit to main, and after a change to the build.
rem --process and --all must be the first argument. Every other argument goes to pytest unchanged, for example:
rem   _scripts\test.cmd tests\catalog -x
rem A -m of your own overrides the tier selection (the last line then names the tiers the script chose, not yours).
rem The last line printed names the tiers that ran. Exit code 5 (nothing selected) prints a note about the tier.
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
%PY% -c "import pytest" >nul 2>nul
if errorlevel 1 (
    >&2 echo [scripts] pytest is not installed in .venv. Run: .venv\Scripts\pip install -r requirements-dev.txt
    exit /b 1
)
set "TIERS=unit"
set "TIER_ARGS=-m unit"
set "PYTEST_ARGS=%*"
set "STRIP="
set "TIER_FLAG="
if /i "%~1"=="--process" (
    set "TIERS=unit, process"
    set "TIER_ARGS=-m "unit or process""
    set "STRIP=1"
    set "TIER_FLAG=--process"
) else if /i "%~1"=="--all" (
    set "TIERS=unit, process, release"
    set "TIER_ARGS="
    set "STRIP=1"
    set "TIER_FLAG=--all"
)
if defined STRIP goto :strip
:strip_done
pushd "%REPO_ROOT%"
%PY% -m pytest -q %TIER_ARGS% %PYTEST_ARGS%
set "RESULT=%ERRORLEVEL%"
popd
echo [scripts] Tiers run: %TIERS%
if "%RESULT%"=="5" echo [scripts] No test was selected in the tiers that ran ^(%TIERS%^). Tests of the process or release tier need --process or --all as the first argument.
exit /b %RESULT%
:strip
rem Remove the tier option from the arguments without splitting the rest (an argument like -o a=b stays whole).
setlocal EnableDelayedExpansion
set "ARGS=%*"
set "ARGS=!ARGS:*%TIER_FLAG%=!"
endlocal & set "PYTEST_ARGS=%ARGS%"
goto :strip_done
