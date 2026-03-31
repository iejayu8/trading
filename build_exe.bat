@echo off
setlocal

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

:: ============================================================
:: build_exe.bat – Package desktop_app.py into a standalone
:: Windows executable using PyInstaller.
::
:: Usage:
::   1. Make sure all dependencies are installed:
::        pip install -r backend\requirements.txt
::        pip install -r requirements_desktop.txt
::        pip install pyinstaller
::   2. Run this script from the repo root:
::        build_exe.bat
::   3. The finished executable will be in the  dist\  folder.
:: ============================================================

echo Installing backend dependencies...
"%PYTHON%" -m pip install --quiet -r backend\requirements.txt
if errorlevel 1 (
  echo ERROR: Failed to install backend dependencies.
  exit /b 1
)

echo Installing desktop dependencies...
"%PYTHON%" -m pip install --quiet -r requirements_desktop.txt
if errorlevel 1 (
  echo ERROR: Failed to install desktop dependencies.
  exit /b 1
)

echo Installing PyInstaller...
"%PYTHON%" -m pip install --quiet pyinstaller
if errorlevel 1 (
  echo ERROR: Failed to install PyInstaller.
  exit /b 1
)

echo.
echo Building BloFin Trading Bot executable...
echo.

if exist "dist\BloFin Trading Bot.exe" (
  del /f /q "dist\BloFin Trading Bot.exe" >nul 2>&1
  if exist "dist\BloFin Trading Bot.exe" (
    echo ERROR: dist\BloFin Trading Bot.exe is in use or cannot be deleted.
    echo Close any running instance of BloFin Trading Bot and retry.
    exit /b 1
  )
)

"%PYTHON%" -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name "BloFin Trading Bot" ^
  --add-data "backend;backend" ^
  --add-data "frontend;frontend" ^
  --hidden-import flask ^
  --hidden-import flask_cors ^
  --hidden-import requests ^
  --hidden-import webview ^
  --exclude-module numba ^
  --exclude-module llvmlite ^
  --exclude-module pytest ^
  --exclude-module _pytest ^
  --exclude-module IPython ^
  desktop_app.py

if errorlevel 1 (
  echo.
  echo Build FAILED. PyInstaller returned an error.
  exit /b 1
)

echo.
if exist "dist\BloFin Trading Bot.exe" (
  echo ============================================================
  echo  Build succeeded!
  echo  Executable: dist\BloFin Trading Bot.exe
  echo.
  echo  Copy the following files alongside the .exe before running:
  echo    - credentials.env
  echo ============================================================
) else (
  echo Build FAILED. Check the output above for errors.
  exit /b 1
)

endlocal
