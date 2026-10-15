@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  Tidy up build junk and temporary audio files.
rem  accounts.json is never touched.
rem ---------------------------------------------------------------------

echo Removing __pycache__ folders...
for /d /r . %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"

echo Removing stray bytecode and temporary audio files...
del /q *.pyc *.pyo 2>nul
del /q voicehive_*.wav 2>nul
del /q "%TEMP%\voicehive_*.wav" 2>nul
del /q "%TEMP%\disvbc_*.wav" 2>nul

echo.
echo Done - the project is clean.
pause
endlocal
