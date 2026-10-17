@echo off
rem Internal helper: select the Python used by the other scripts and set REPO_ROOT.
rem   call "%~dp0_python.cmd"      ->  PY (a command line), REPO_ROOT (no trailing backslash)
rem Order: the repository's .venv, the bundled runtime in src\gupkg\python, then py -3 / python.
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "PY="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PY="%REPO_ROOT%\.venv\Scripts\python.exe""
if not defined PY if exist "%REPO_ROOT%\src\gupkg\python\python.exe" set "PY="%REPO_ROOT%\src\gupkg\python\python.exe""
if not defined PY (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    >&2 echo [scripts] No Python found. Create .venv or restore src\gupkg\python.
    exit /b 9009
)
exit /b 0
