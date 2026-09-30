@echo off
rem One-time setup: installs the package needed to sign requests with a Kalshi API key.
rem (The scanner itself runs on plain Python; this is only for the optional key.)
title Arb Scanner - setup
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found on PATH. Install Python 3.10+ from python.org. & pause & exit /b 1)
python -m pip install --upgrade cryptography
echo.
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created %~dp0.env - put your Kalshi key ID in it.
)
echo Setup finished.
pause
