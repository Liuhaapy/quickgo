@echo off
REM Refresh the form N seconds before submit time. Increase to 2 or 3 if the page loads slowly.
set REFRESH_BEFORE=1
call "%~dp0run_with_pause.bat" --fast-open --refresh-before %REFRESH_BEFORE% %*
