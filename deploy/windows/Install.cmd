@echo off
rem Monitor Agent one-click setup: requests administrator rights and runs setup.ps1.
rem The script asks for the server address and API token; nothing secret ships in the package.
setlocal
set "SETUP=%~dp0deploy\windows\setup.ps1"
if not exist "%SETUP%" set "SETUP=%~dp0setup.ps1"
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%SETUP%" %*
echo.
pause
