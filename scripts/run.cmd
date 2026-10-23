@echo off
rem Run gupkg from the source tree for manual testing.   scripts\run.cmd [gupkg arguments]
rem It starts src\gupkg\gupkg.cmd, so gupkg runs on its bundled Python like it does for a user.
"%~dp0..\src\gupkg\gupkg.cmd" %*
exit /b %ERRORLEVEL%
