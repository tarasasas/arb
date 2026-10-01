@echo off
rem One-time: keep cash on every Kalshi exchange shard (crypto is shard 2), so orders there do not fail.
cd /d "%~dp0"
python -m arb.shards
echo.
pause
