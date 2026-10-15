@echo off
rem ---------------------------------------------------------------------------
rem VoiceHive - tidy up build junk and temporary audio files.
rem This never touches accounts.json.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

echo Cleaning __pycache__ folders...
for /d /r "%~dp0" %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d"
)

echo Removing stray bytecode and audio dumps...
del /q "*.pyc" 2>nul
del /q "*.pyo" 2>nul
del /q "voicehive_*.wav" 2>nul
del /q "%TEMP%\voicehive_*.wav" 2>nul
del /q "%TEMP%\disvbc_*.wav" 2>nul

echo.
echo Done. Project is clean.
pause
