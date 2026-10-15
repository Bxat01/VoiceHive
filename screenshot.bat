@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  Refreshes the screenshots in docs\ by capturing the app's own windows.
rem  The app is opened without signing any account in, so it is safe to run
rem  this while you already have sessions open somewhere else.
rem ---------------------------------------------------------------------

set "PY=py -3.13"
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 set "PY=python"

echo Capturing the VoiceHive windows - they will flash on screen briefly.
echo.
%PY% docs\capture_screenshots.py
if errorlevel 1 (
    echo.
    echo   The capture failed. Make sure Python 3.13 and discord.py-self
    echo   are installed, then try again.
)

echo.
pause
endlocal
