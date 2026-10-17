@echo off
rem Build the self-contained release bundle.   scripts\build-standalone.cmd [output.zip]
rem Downloads the pinned embeddable Python once (SHA-256 verified, kept in dist\cache), installs the
rem locked dependencies into it, and writes dist\gupkg-<version>.zip unless an output path is given.
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"

set "PY_VERSION_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
set "PY_SHA256=4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3"
set "RUNTIME=%REPO_ROOT%\dist\cache\python-3.12.10-embed-amd64.zip"
if not exist "%REPO_ROOT%\dist\cache" mkdir "%REPO_ROOT%\dist\cache"

if not exist "%RUNTIME%" (
    echo [build] Downloading embedded Python 3.12.10...
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri $env:PY_VERSION_URL -OutFile $env:RUNTIME" || (popd & exit /b 1)
)
for /f "delims=" %%H in ('"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath $env:RUNTIME).Hash"') do set "ACTUAL=%%H"
if /I not "%ACTUAL%"=="%PY_SHA256%" (
    >&2 echo [build] The downloaded runtime failed SHA-256 verification; deleting it.
    del "%RUNTIME%"
    popd & exit /b 1
)

set "OUTPUT=%~1"
if defined OUTPUT (set "OUTPUT_ARGUMENT=--output "%OUTPUT%"") else (set "OUTPUT_ARGUMENT=")
%PY% tools\build_standalone.py --runtime "%RUNTIME%" --runtime-sha256 %PY_SHA256% %OUTPUT_ARGUMENT% || (popd & exit /b 1)
echo [build] Done. The bundle is in dist\ unless you gave an output path.
popd
exit /b 0
