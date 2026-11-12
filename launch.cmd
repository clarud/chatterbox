@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.12 or later with Tk support and add it to PATH.
    pause
    exit /b 1
)
start "" pythonw -m chatterbox %*
