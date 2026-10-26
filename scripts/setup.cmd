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
rem Create or repair the development environment (.venv).   scripts\setup.cmd [python.exe] [--force]
rem .venv must contain pip (the build scripts and tools\update_deps.py use it; the release bundle has no
rem pip) and the packages of requirements-dev.txt. This script makes sure it does:
rem   - No .venv, or one without pip: it is removed and created again. uv is tried first
rem     ("uv venv --seed", which includes pip); if uv is missing or fails, "python -m venv" is used.
rem   - A good .venv: the packages of requirements-dev.txt are installed or brought up to the pinned versions.
rem   - --force: remove and create it again even if it looks good.
rem [python.exe] is the interpreter that creates .venv (uv picks one itself when it is not given;
rem the fallback uses "python" from PATH). gupkg itself never uses it: it runs only on the interpreter
rem bundled in its own folder. The other scripts call this one by themselves when .venv is missing.
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "BASE="
set "FORCE="
for %%A in (%*) do (
    if /I "%%~A"=="--force" (set "FORCE=1") else if not defined BASE set "BASE=%%~A"
)
set "VENV=%REPO_ROOT%\.venv"
set "VENVPY=%VENV%\Scripts\python.exe"
pushd "%REPO_ROOT%"

set "GOOD="
if not defined FORCE if exist "%VENVPY%" ("%VENVPY%" -c "import pip" >nul 2>nul && set "GOOD=1")
if defined GOOD goto :install

if exist "%VENV%" (
    echo [setup] Removing the old environment: %VENV%
    rmdir /s /q "%VENV%"
    if exist "%VENV%" (>&2 echo [setup] Could not remove %VENV%. Close programs that use it and try again. & popd & exit /b 1)
)

set "MADE="
where uv >nul 2>nul
if not errorlevel 1 (
    echo [setup] Creating %VENV% with uv ^(uv venv --seed^)
    if defined BASE (call uv venv --seed --python "%BASE%" "%VENV%") else (call uv venv --seed "%VENV%")
    if not errorlevel 1 set "MADE=1"
    if not defined MADE echo [setup] uv failed; trying python -m venv
)
if not defined MADE (
    if exist "%VENV%" rmdir /s /q "%VENV%"
    if not defined BASE set "BASE=python"
    echo [setup] Creating %VENV% with "%BASE%" -m venv
    call "%BASE%" -m venv "%VENV%" || (>&2 echo [setup] Could not create the environment. Install uv, or pass the path of a full Python: scripts\setup.cmd C:\path\python.exe & popd & exit /b 1)
)

:install
echo [setup] Installing requirements-dev.txt
"%VENVPY%" -m pip install --disable-pip-version-check -r requirements-dev.txt || (popd & exit /b 1)
"%VENVPY%" -c "import pip, pytest, textual, tomli_w; print('[setup] Ready: Python', __import__('sys').version.split()[0], '- pip', pip.__version__, '- pytest', pytest.__version__)" || (popd & exit /b 1)
echo [setup] Next: scripts\test.cmd
popd
exit /b 0
