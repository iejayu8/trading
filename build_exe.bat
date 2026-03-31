@echo off
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

echo Installing PyInstaller...
pip install --quiet pyinstaller

echo.
echo Building BloFin Trading Bot executable...
echo.

pyinstaller ^
  --onefile ^
  --windowed ^
  --name "BloFin Trading Bot" ^
  --add-data "backend;backend" ^
  --add-data "frontend;frontend" ^
  --hidden-import flask ^
  --hidden-import flask_cors ^
  --hidden-import pandas_ta ^
  --hidden-import requests ^
  --hidden-import webview ^
  desktop_app.py

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
