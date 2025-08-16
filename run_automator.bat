@echo off
:: This changes the directory to the location of the batch file
cd /d "%~dp0"

:: Use pythonw.exe to hide console by default; fallback to python.exe if needed
if exist .\venv\Scripts\pythonw.exe (
	.\venv\Scripts\pythonw.exe twitch_drop_automator.py
) else (
	.\venv\Scripts\python.exe twitch_drop_automator.py
)