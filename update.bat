@echo off
rem Updates this folder to the latest version from GitHub by running update.sh in Git Bash.
rem Keeps your .env, kalshi.key and trades.jsonl. Throws away any other edits you made to the code.
rem "update.bat auto" (used by start-dashboard.bat) runs without pausing.
setlocal
cd /d "%~dp0"
set "AUTO=%~1"

rem Find Git Bash: where Git for Windows says it is installed (registry), the usual folders, then PATH.
set "BASH="
for %%K in ("HKLM\SOFTWARE\GitForWindows" "HKCU\SOFTWARE\GitForWindows" "HKLM\SOFTWARE\WOW6432Node\GitForWindows") do if not defined BASH for /f "tokens=2,*" %%A in ('reg query %%K /v InstallPath 2^>nul ^| find "InstallPath"') do if exist "%%B\bin\bash.exe" set "BASH=%%B\bin\bash.exe"
for %%B in ("%ProgramFiles%\Git\bin\bash.exe" "%ProgramFiles(x86)%\Git\bin\bash.exe" "%LocalAppData%\Programs\Git\bin\bash.exe" "%UserProfile%\scoop\apps\git\current\bin\bash.exe") do if not defined BASH if exist %%B set "BASH=%%~B"
if not defined BASH for /f "delims=" %%G in ('where git 2^>nul') do if not defined BASH if exist "%%~dpG..\bin\bash.exe" set "BASH=%%~dpG..\bin\bash.exe"
if not defined BASH (
  echo Git Bash was not found, so the code was not updated.
  echo Install Git for Windows from https://git-scm.com/download/win ^(default options^).
  if /i not "%AUTO%"=="auto" pause
  exit /b 1
)

"%BASH%" ./update.sh
if errorlevel 1 (echo. & echo Update failed: see the messages above. Starting the version you already have.)
if /i not "%AUTO%"=="auto" (echo. & pause)
exit /b 0
