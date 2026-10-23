@echo off
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~f0") do set "GUPKG_SCRIPT_DIR=%%~dpI"

rem gupkg runs only on the Python inside its own version directory. No PATH
rem lookup, no py launcher, and no GUPKG_PYTHON or gupkg.python override is ever
rem consulted: whatever the caller put in GUPKG_PYTHON is replaced below.

rem fltmc.exe succeeds only in an elevated process. A failed probe (including a
rem missing tool) selects an ordinary launch; GUPKG_FORCE_ELEVATED can only
rem make the launch stricter, never weaker.
set "GUPKG_ELEVATED_FLAG="
"%SystemRoot%\System32\fltmc.exe" >nul 2>nul
if not errorlevel 1 set "GUPKG_ELEVATED_FLAG=--elevated"
if defined GUPKG_FORCE_ELEVATED set "GUPKG_ELEVATED_FLAG=--elevated"

rem cmd.exe /c quoting can leave a leading quote on forwarded arguments when
rem this launcher is started by the native shim. Normalize every argument
rem through %%~1 before handing the final list to Python.
set "GUPKG_FORWARD_ARGS="
:collect_forward_args
if "%~1"=="" goto :forward_args_ready
set "GUPKG_FORWARD_ARGS=%GUPKG_FORWARD_ARGS% "%~1""
shift
goto :collect_forward_args
:forward_args_ready

rem Run isolated (-I: ignore PYTHON* variables, the script directory and the
rem current directory; -s: no user site; -B: write no bytecode).
set "GUPKG_PYTHON=%GUPKG_SCRIPT_DIR%python\python.exe"
if not exist "%GUPKG_PYTHON%" (
    >&2 echo [gupkg] The bundled Python is missing: %GUPKG_PYTHON%
    >&2 echo [gupkg] Reinstall gupkg from a release ZIP.
    exit /b 1
)
"%GUPKG_PYTHON%" -I -s -B "%GUPKG_SCRIPT_DIR%bootstrap.py" %GUPKG_ELEVATED_FLAG% %GUPKG_FORWARD_ARGS%
set "GUPKG_EXIT_CODE=%ERRORLEVEL%"
exit /b %GUPKG_EXIT_CODE%
