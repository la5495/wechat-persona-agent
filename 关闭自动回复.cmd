@echo off
cd /d "%~dp0"
echo ============================================================
echo   Stop WeChat auto-reply service
echo ============================================================
".venv\Scripts\python.exe" service.py stop %*
echo.
pause
