@echo off
title Screen Matcher
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [setup] First run: installing packages, this takes 1-2 minutes...
    python -m venv .venv
    if errorlevel 1 goto fail
    ".venv\Scripts\python.exe" -m pip install -q numpy opencv-python-headless mss sv-ttk
    if errorlevel 1 goto fail
)
start "" ".venv\Scripts\pythonw.exe" screen_matcher.py
exit /b 0

:fail
echo.
echo [error] Setup failed. Install Python 3.10+ from python.org and check "Add python.exe to PATH".
pause
exit /b 1
