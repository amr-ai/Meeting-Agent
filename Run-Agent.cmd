@echo off
setlocal
cd /d "%~dp0"
title Meeting Voice Agent
where py >nul 2>nul
if not errorlevel 1 (
    py -3 scripts\launch.py
) else (
    python scripts\launch.py
)
if errorlevel 1 (
    echo.
    echo Startup did not complete. Read the message above.
    pause
)
endlocal
