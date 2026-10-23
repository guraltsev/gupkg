@echo off
rem Run the test suite.   scripts\test.cmd [pytest arguments]
rem Example: scripts\test.cmd tests\test_publish_registry.py -x
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
%PY% -c "import pytest" >nul 2>nul
if errorlevel 1 (
    >&2 echo [scripts] pytest is not installed in .venv. Run: .venv\Scripts\pip install -r requirements-dev.txt
    exit /b 1
)
pushd "%REPO_ROOT%"
%PY% -m pytest -q %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
