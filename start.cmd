@echo off
rem Starts Rental finder and opens it in your browser. Close this window to stop it.
cd /d "%~dp0"
start "" http://127.0.0.1:8765
"%USERPROFILE%\.venvs\rental-finder\Scripts\python.exe" server.py
pause
