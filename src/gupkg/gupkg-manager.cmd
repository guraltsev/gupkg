@echo off
rem gupkg-manager: the manager program of this gupkg (`gupkg manager ...` starts this launcher; it is not the package launcher of a version directory).
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~f0") do set "GUPKG_SCRIPT_DIR=%%~dpI"

rem gupkg runs only on the Python inside its own version directory. No PATH
rem lookup, no py launcher and no override variable or file is ever consulted,
rem and elevated and ordinary runs start the same interpreter with the same flags
rem (bootstrap.py detects elevation itself).
set "GUPKG_PY=%GUPKG_SCRIPT_DIR%python\python.exe"
if not exist "%GUPKG_PY%" (
    >&2 echo [gupkg] The bundled Python is missing: %GUPKG_PY%
    >&2 echo [gupkg] Reinstall gupkg from a release ZIP.
    exit /b 1
)

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
rem current directory; -s: no user site; -B: write no bytecode). The first argument
rem tells the bootstrap to start the manager program instead of the gupkg command line.
"%GUPKG_PY%" -I -s -B -S "%GUPKG_SCRIPT_DIR%bootstrap.py" --gupkg-program=manager %GUPKG_FORWARD_ARGS%
exit /b %ERRORLEVEL%
