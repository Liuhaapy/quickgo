@echo off
cd /d "%~dp0"
if not exist "config.backup.json" copy /Y "config.json" "config.backup.json" >nul
start "" notepad "%~dp0config.json"
