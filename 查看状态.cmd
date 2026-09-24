@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" service.py status %*
echo.
pause
