@echo off
call "%~dp0run_with_pause.bat" --config "%~dp0tests\test_locked_config.json" --target-in 8 --no-pause --headless --fast-open
