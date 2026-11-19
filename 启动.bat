@echo off
cd /d "%~dp0"
setlocal

where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo [ERROR] Python not found in PATH.
  echo Install Python 3.10+ from https://www.python.org/downloads/
  echo During install, tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

set "PORT=8090"

echo ==========================================
echo   Watermark Remover - Douyin / Xiaohongshu
echo   http://127.0.0.1:8090
echo   Close this window to stop the server.
echo ==========================================
echo.

python -c "import flask, requests, websocket" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies, first run only...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed. Check network or use a mirror.
    pause
    exit /b 1
  )
)

start "" "http://127.0.0.1:8090"
python app.py
pause
