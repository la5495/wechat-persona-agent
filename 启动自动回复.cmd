@echo off
cd /d "%~dp0"
echo ============================================================
echo   Start WeChat auto-reply service
echo ============================================================
".venv\Scripts\python.exe" service.py start %*
echo.
echo   NOTE: service starts in DRAFT-ONLY mode.
echo         Send "kaishi" (Chinese: kai shi) to WeChat File Transfer
echo         Helper to enable real sending.
echo.
pause
