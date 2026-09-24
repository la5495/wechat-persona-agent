@echo off
REM ==========================================================================
REM  Hollow-needle frida injection test  (WeChat 4.x send-leg feasibility)
REM
REM  THIS MUST RUN AS ADMINISTRATOR (elevated).
REM  Reason (measured): our non-elevated token cannot obtain VM_WRITE /
REM  VM_OPERATION / CREATE_THREAD on Weixin.exe (elevated process) -> err=5.
REM
REM  The frida agent hooks NOTHING and writes NOTHING. It only reports what it
REM  can see about its own host process, then is unloaded cleanly.
REM
REM  This file is deliberately ALL-ASCII: cmd.exe reads .cmd as ANSI and the
REM  project folder name is non-ASCII. %~dp0 is expanded at runtime.
REM ==========================================================================
setlocal
set "HERE=%~dp0"
set "PY=%HERE%..\.venv\Scripts\python.exe"

echo ==========================================================
echo  Hollow-needle injection test
echo  time: %DATE% %TIME%
echo  user: %USERNAME%
echo ==========================================================

if not exist "%PY%" (
  echo [ERROR] python not found: %PY%
  echo         run: uv venv --python 3.12 .venv
  pause
  exit /b 5
)

echo Running injector with: %PY%
echo.

"%PY%" "%HERE%inject.py" %*
set RC=%ERRORLEVEL%

echo.
echo ---- exit code: %RC% ----
echo Log: %HERE%hollow_needle.log
echo.
pause
