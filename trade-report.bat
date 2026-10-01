@echo off
rem Report on what happened to some trades (app log + Kalshi fills and settlements). Saves trade-report.txt.
cd /d "%~dp0"
set /p Q=Search (Enter for NPB, Japan baseball): 
python -m arb.report %Q%
echo.
pause
