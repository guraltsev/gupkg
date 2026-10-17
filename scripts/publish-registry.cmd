@echo off
rem Check the registry and move and push the "registry" Git tag.
rem   scripts\publish-registry.cmd [--target REV] [--force] [--remote NAME]
rem Push your commits first: only a revision that is on a remote branch is published.
rem --target publishes REV instead of HEAD; --force publishes even if a check fails.
setlocal EnableExtensions
call "%~dp0_python.cmd" || exit /b %ERRORLEVEL%
pushd "%REPO_ROOT%"
%PY% tools\publish_registry.py %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
