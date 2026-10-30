@echo off
rem gupkg-tui: the tui program of this gupkg (`gupkg tui ...` starts this launcher; it is not the package launcher of a version directory).
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

rem The arguments are forwarded as %* (the text the caller typed), never rebuilt one by one: a loop over %~1 splits an
rem argument at =, comma and semicolon, turns an empty argument into nothing, and lets & | > inside quotes run as commands.

rem Run isolated (-I: ignore PYTHON* variables, the script directory and the
rem current directory; -s: no user site; -B: write no bytecode). The first argument
rem tells the bootstrap to start the tui program instead of the gupkg command line.
"%GUPKG_PY%" -I -s -B -S "%GUPKG_SCRIPT_DIR%bootstrap.py" --gupkg-program=tui %*
exit /b %ERRORLEVEL%
