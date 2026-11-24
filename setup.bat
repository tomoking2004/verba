@echo off
rem Verba setup (run once). See scripts\setup.ps1 for details.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
pause
