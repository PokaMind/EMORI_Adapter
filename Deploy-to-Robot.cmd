@echo off
rem One-click EMORI deploy. Double-click it while the robot is on the same Wi-Fi as this
rem PC. All the work is in scripts\deploy_to_robot.py; this file only finds a Python.
setlocal
cd /d "%~dp0"
set "PY="
if exist ".venv-reachy\Scripts\python.exe" set "PY=.venv-reachy\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
rem "python" can be the Microsoft Store stub, which exits 9009 instead of running.
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH"^), then double-click this file again.
  pause
  exit /b 1
)
%PY% scripts\deploy_to_robot.py %*
set "RC=%ERRORLEVEL%"
echo.
if "%~1"=="" pause
exit /b %RC%
