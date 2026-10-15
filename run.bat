@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  VoiceHive launcher.
rem  Looks for Python 3.13, makes sure discord.py-self is installed, then
rem  starts the app. Nothing here quits silently: if something is missing
rem  you get the message on screen plus a pause.
rem ---------------------------------------------------------------------

set "PY=py -3.13"
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 (
    set "PY=python"
    python -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   Python was not found.
        echo.
        echo   Install Python 3.13 from https://www.python.org/downloads/
        echo   and tick "Add python.exe to PATH" while installing.
        echo.
        pause
        exit /b 1
    )
)

rem --- dependencies -----------------------------------------------------
%PY% -c "import discord, davey" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Installing discord.py-self with voice support - one time only...
    echo.
    %PY% -m pip install "discord.py-self[voice]"
    if errorlevel 1 (
        echo.
        echo   The install failed. Check your internet connection, then run:
        echo.
        echo       %PY% -m pip install "discord.py-self[voice]"
        echo.
        pause
        exit /b 1
    )
)

echo Starting VoiceHive...
%PY% main.py

if errorlevel 1 (
    echo.
    echo   VoiceHive exited with an error - the message is just above.
    echo.
    pause
)

endlocal
