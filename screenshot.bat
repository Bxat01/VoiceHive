@echo off
rem Take real PNG screenshots of the VoiceHive windows into docs\.
rem The app opens without signing any account in, so it is safe to run while
rem you already have sessions open elsewhere.
cd /d "%~dp0"

py -3.13 docs\capture_screenshots.py
if errorlevel 1 (
    echo.
    echo Something went wrong. Make sure Python 3.13 and the dependency
    echo (discord.py-self[voice]) are installed, then try again.
)
pause
