@echo off
rem Updates this folder to the latest version from GitHub by running update.sh in Bash.
rem Keeps your .env, kalshi.key and trades.jsonl. Throws away any other edits you made to the code.
title Arb Scanner - update
cd /d "%~dp0"
echo Close the dashboard window first if it is running.
echo.

rem Find Bash: Git Bash in its usual install folders first, then any bash on PATH (e.g. WSL).
set "BASH="
for %%B in ("%ProgramFiles%\Git\bin\bash.exe" "%ProgramFiles(x86)%\Git\bin\bash.exe" "%LocalAppData%\Programs\Git\bin\bash.exe") do if not defined BASH if exist %%B set "BASH=%%~B"
if not defined BASH for /f "delims=" %%B in ('where bash 2^>nul') do if not defined BASH set "BASH=%%B"
if not defined BASH (
  echo Bash was not found. Install Git for Windows from https://git-scm.com/download/win
  echo ^(default options^), then run this again.
  pause
  exit /b 1
)

"%BASH%" ./update.sh
if errorlevel 1 (echo. & echo Update failed: see the messages above.)
echo.
pause
