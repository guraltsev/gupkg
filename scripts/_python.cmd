@echo off
rem Internal helper: select the Python used by the other scripts and set REPO_ROOT.
rem   call "%~dp0_python.cmd"      ->  PY (a quoted command), REPO_ROOT (no trailing backslash)
rem Only the repository's .venv is used. Nothing is searched for: no committed runtime, no py, no PATH.
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "PY="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PY="%REPO_ROOT%\.venv\Scripts\python.exe""
if not defined PY (
    >&2 echo [scripts] The development environment is missing: %REPO_ROOT%\.venv
    >&2 echo [scripts] Create it: ^<python^> -m venv .venv ^&^& .venv\Scripts\pip install -r requirements-dev.txt
    exit /b 9009
)
exit /b 0
