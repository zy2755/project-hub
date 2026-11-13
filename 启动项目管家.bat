@echo off
chcp 65001 >nul
title Project Hub
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
  echo.
  echo   [!] Python not found on this machine.
  echo       Install Python 3.10+ from https://www.python.org/downloads/
  echo       and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

echo Starting Project Hub...
%PY% server.py

if errorlevel 1 (
  echo.
  echo   [!] The server exited with an error. Scroll up to see the details.
  pause
)
