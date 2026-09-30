@echo off
rem Runs the unit tests (payout math, fees, parsing, key signing).
title Arb Scanner - tests
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found on PATH. Install Python 3.10+ from python.org. & pause & exit /b 1)
python -m unittest discover -s tests -t . -v
echo.
pause
