@echo off
rem Gets the latest code, then starts the scanner and opens the dashboard at http://localhost:8791
rem Close this window (or press Ctrl+C) to stop it. To skip the update: start-dashboard.bat --no-update
rem The block below is parsed in one go, so it is safe for the update to rewrite this file.
if /i not "%~1"=="--no-update" (
  title Arb Scanner - updating
  cd /d "%~dp0"
  call "%~dp0update.bat" auto
  "%~f0" --no-update %*
  exit /b
)
shift
title Arb Scanner
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found on PATH. Install Python 3.10+ from python.org. & pause & exit /b 1)
rem Packages for API keys and live price streams; installed once, skipped after that.
python -c "import websocket, cryptography" >nul 2>nul || python -m pip install --quiet cryptography websocket-client
python -m arb %1 %2 %3 %4 %5 %6
echo.
echo Scanner stopped.
pause
