@echo off
rem Times each step of a trade on Kalshi and Polymarket, with 1-share 1-cent test orders that should not fill.
cd /d "%~dp0"
python -m arb.latency
echo.
pause
