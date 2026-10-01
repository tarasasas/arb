@echo off
rem One-time: ask Kalshi for the free Advanced API tier (faster scanning). See README.
cd /d "%~dp0"
python -m arb.upgrade
echo.
pause
