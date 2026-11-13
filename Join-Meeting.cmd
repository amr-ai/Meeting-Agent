@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title AI Meeting Agent
if not exist ".venv\Scripts\python.exe" (
    echo Run Run-Agent.cmd once to prepare the Python environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" scripts\meeting_tunnel.py
if errorlevel 1 pause
endlocal
