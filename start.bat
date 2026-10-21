@echo off
REM Difaf Al-Arabiya - double-click to run on Windows
setlocal
cd /d "%~dp0"
title Difaf Al-Arabiya

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo  Python is not installed.
  echo  Download Python 3.12 from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" during installation.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create the virtual environment.
    pause
    exit /b 1
  )
)

fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
  echo Installing requirements - the first run needs internet and may take a minute...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo Could not install the requirements. Check the internet connection and try again.
    pause
    exit /b 1
  )
  copy /y requirements.txt ".venv\requirements.installed" >nul
)

echo.
echo  Starting... the browser will open automatically.
echo  Keep this window open while the competition is running.
echo.
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8000/"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
