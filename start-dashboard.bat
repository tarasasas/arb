@echo off
rem Starts the arbitrage scanner and opens the dashboard at http://localhost:8791
rem Close this window (or press Ctrl+C) to stop it.
title Arb Scanner
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found on PATH. Install Python 3.10+ from python.org. & pause & exit /b 1)
python -m arb %*
echo.
echo Scanner stopped.
pause
