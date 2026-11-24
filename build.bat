@echo off
rem Build a standalone exe for distribution. See scripts\build.ps1 for details.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build.ps1"
pause
