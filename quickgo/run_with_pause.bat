@echo off
chcp 65001 >nul
title WPS Form Assistant
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
echo.
echo ============================================================
echo Done. Press any key to close this window.
echo ============================================================
pause >nul
