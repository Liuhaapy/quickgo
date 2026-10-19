@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "config.backup.json" (
    echo No backup found: config.backup.json
    pause
    exit /b 1
)
copy /Y "config.backup.json" "config.json" >nul
echo config.json restored from config.backup.json
pause
