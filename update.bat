@echo off
rem Updates this folder to the latest version from GitHub (branch claude/elegant-bell-qeagk3).
rem Keeps your .env, kalshi.key and trades.jsonl. Throws away any other edits you made to the code.
title Arb Scanner - update
cd /d "%~dp0"

rem Find git: on PATH, or where Git for Windows installs it (Git Bash has it even when PATH does not).
set "GIT=git"
git --version >nul 2>nul && goto found
for %%G in ("%ProgramFiles%\Git\cmd\git.exe" "%ProgramFiles(x86)%\Git\cmd\git.exe" "%LocalAppData%\Programs\Git\cmd\git.exe" "%ProgramFiles%\Git\bin\git.exe") do if exist %%G (set "GIT=%%~G" & goto found)
rem GitHub Desktop ships its own git.
for /d %%D in ("%LocalAppData%\GitHubDesktop\app-*") do if exist "%%~D\resources\app\git\cmd\git.exe" set "GIT=%%~D\resources\app\git\cmd\git.exe"
if not "%GIT%"=="git" goto found
echo Git was not found from here. Either run ./update.sh in the terminal where git works,
echo or install Git from https://git-scm.com/download/win and run this again.
pause
exit /b 1

:found
set BRANCH=claude/elegant-bell-qeagk3
echo Close the dashboard window first if it is running.
echo.
echo Getting the latest changes from %BRANCH% ...
"%GIT%" fetch origin %BRANCH%
if errorlevel 1 (echo Could not download from GitHub. Check your internet connection. & pause & exit /b 1)
"%GIT%" checkout -q -f main
if errorlevel 1 (echo Could not switch to main. & pause & exit /b 1)
"%GIT%" reset -q --hard FETCH_HEAD
if errorlevel 1 (echo Update failed. & pause & exit /b 1)
echo.
"%GIT%" log -1 --format="Up to date: %%h %%s (%%cr)"
echo.
echo Next: run run-tests.bat, then start-dashboard.bat.
pause
