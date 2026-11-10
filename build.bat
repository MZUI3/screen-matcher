@echo off
title Build Screen Matcher (ScreenMatcher.exe)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto fail
)
echo [2/3] Installing packages...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
echo [3/3] Building exe (1-3 minutes)...
set "EXTRA="
if exist "presets\" set "EXTRA=--add-data presets;presets"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole --uac-admin --name ScreenMatcher --add-data "assets;assets" %EXTRA% screen_matcher.py
if errorlevel 1 goto fail
echo.
echo Done: dist\ScreenMatcher.exe
pause
exit /b 0

:fail
echo.
echo [error] Build failed. Install Python 3.10+ from python.org and check "Add python.exe to PATH".
pause
exit /b 1
