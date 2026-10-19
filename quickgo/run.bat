@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set EXITCODE=%ERRORLEVEL%
if "%~1"=="" goto pause_end
if not "%EXITCODE%"=="0" goto pause_end
goto end
:pause_end
pause
:end
exit /b %EXITCODE%
