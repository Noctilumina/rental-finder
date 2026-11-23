@echo off
rem Starts Rental finder and opens it in your browser (or just opens it if it is already running).
rem Close this window to stop the app; a running refresh keeps going.
cd /d "%~dp0"
"%USERPROFILE%\.venvs\rental-finder\Scripts\python.exe" server.py
if errorlevel 1 pause
