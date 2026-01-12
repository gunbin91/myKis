@echo off
chcp 65001 >nul
setlocal

REM Headless scheduler start (Windows)
REM - 웹페이지/브라우저 없이도 mock/real 스케줄러를 동시에 실행합니다.

echo ==========================================
echo myKis headless scheduler start
echo ==========================================

cd /d "%~dp0\.."
if errorlevel 1 (
  echo [Error] Failed to move to project root.
  pause
  exit /b 1
)

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

echo [Info] Installing requirements...
"%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [Error] requirements install failed.
  pause
  exit /b 1
)

echo ==========================================
echo Starting headless scheduler (mock/real)...
echo Stop with Ctrl+C.
echo ==========================================

"%VENV_DIR%\Scripts\python.exe" run\start_headless.py

echo.
echo [Info] Press any key to close...
pause

