@echo off
rem Removes the Monitor Agent tasks and runtimes. Configuration, logs and spool are kept;
rem run "uninstall.ps1 -Purge" from an elevated PowerShell to remove them too.
setlocal
set "UNINSTALL=%~dp0deploy\windows\uninstall.ps1"
if not exist "%UNINSTALL%" set "UNINSTALL=%~dp0uninstall.ps1"
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%UNINSTALL%"
echo.
pause
