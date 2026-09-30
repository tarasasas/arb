@echo off
rem Runs one full scan and prints the opportunities in this window (no dashboard).
title Arb Scanner - single scan
cd /d "%~dp0"
where python >nul 2>nul || (echo Python was not found on PATH. Install Python 3.10+ from python.org. & pause & exit /b 1)
python -m arb --once
echo.
pause
