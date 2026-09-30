@echo off
rem Updates this folder to the latest version from GitHub (branch claude/elegant-bell-qeagk3).
rem Keeps your .env, kalshi.key and trades.jsonl. Throws away any other edits you made to the code.
title Arb Scanner - update
cd /d "%~dp0"
where git >nul 2>nul || (echo Git was not found on PATH. Install it from git-scm.com. & pause & exit /b 1)
set BRANCH=claude/elegant-bell-qeagk3
echo Close the dashboard window first if it is running.
echo.
echo Getting the latest changes from %BRANCH% ...
git fetch origin %BRANCH% || (echo Could not reach GitHub. Check your internet connection. & pause & exit /b 1)
git checkout -q -f main || (echo Could not switch to main. & pause & exit /b 1)
git reset -q --hard FETCH_HEAD || (echo Update failed. & pause & exit /b 1)
echo.
git log -1 --format="Up to date: %%h %%s (%%cr)"
echo.
echo Next: run run-tests.bat, then start-dashboard.bat.
pause
