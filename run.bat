@echo off
rem Launch VoiceHive with Python 3.13
rem (the interpreter that has discord.py-self installed).
cd /d "%~dp0"

py -3.13 -c "import discord" 2>nul
if errorlevel 1 (
    echo Installing discord.py-self...
    py -3.13 -m pip install "discord.py-self[voice]"
)

py -3.13 -c "import davey" 2>nul
if errorlevel 1 (
    echo Installing voice (DAVE / E2EE) support...
    py -3.13 -m pip install "discord.py-self[voice]"
)

py -3.13 main.py
if errorlevel 1 (
    echo.
    echo Python 3.13 with discord.py-self was not found. Trying default python...
    python main.py
    if errorlevel 1 pause
)
