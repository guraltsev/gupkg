@echo off
rem Internal helper: select the Python used by the other scripts and set REPO_ROOT.
rem   call "%~dp0_python.cmd"      ->  PY (a quoted command), REPO_ROOT (no trailing backslash)
rem Only the repository's .venv is used. Nothing is searched for: no committed runtime, no py, no PATH.
rem A missing .venv is created first by scripts\setup.cmd (uv, then python -m venv).
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
if not exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    echo [scripts] The development environment is missing; creating it with scripts\setup.cmd
    call "%~dp0setup.cmd"
    if errorlevel 1 exit /b
)
set "PY="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PY="%REPO_ROOT%\.venv\Scripts\python.exe""
if not defined PY (
    >&2 echo [scripts] The development environment is missing: %REPO_ROOT%\.venv. Run scripts\setup.cmd
    exit /b 9009
)
exit /b 0
