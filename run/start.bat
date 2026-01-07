@echo off
chcp 65001 >nul
setlocal

REM myKis environment setup and start (Windows)

echo ==========================================
echo myKis environment setup and start
echo ==========================================

REM Move to project root directory
cd /d "%~dp0\.."
if errorlevel 1 (
  echo [Error] Failed to move to project root.
  pause
  exit /b 1
)

REM Pick python (prefer py launcher)
set "PY_CMD="
py -3 --version >nul 2>&1
if %errorlevel%==0 (
  set "PY_CMD=py -3"
) else (
  python --version >nul 2>&1
  if %errorlevel%==0 (
    set "PY_CMD=python"
  )
)

if "%PY_CMD%"=="" (
  echo [Error] Python not found. Please install Python and ensure it is on PATH.
  pause
  exit /b 1
)

echo [Info] Python: %PY_CMD%

REM Create venv if needed
set "VENV_DIR=venv"
if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [Info] Creating venv...
  %PY_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [Error] venv creation failed.
    pause
    exit /b 1
  )
)

REM Upgrade pip + install deps using venv python
echo [Info] Upgrading pip...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo [Error] pip upgrade failed.
  pause
  exit /b 1
)

echo [Info] Installing requirements...
"%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [Error] requirements install failed.
  pause
  exit /b 1
)

REM Validate flask import (ensures venv is really used)
"%VENV_DIR%\Scripts\python.exe" -c "import flask; print('flask_ok', flask.__version__)" 1>nul 2>nul
if errorlevel 1 (
  echo [Error] Flask import failed. Check venv and requirements install.
  pause
  exit /b 1
)

echo ==========================================
echo Starting server...
echo Default port is 7500. If busy, it will try 7501+ automatically.
echo Stop with Ctrl+C.
echo ==========================================

"%VENV_DIR%\Scripts\python.exe" run\start.py

echo.
echo [Info] Press any key to close...
pause

