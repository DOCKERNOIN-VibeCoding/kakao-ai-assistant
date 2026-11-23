@echo off
rem Kakao AI Assistant installer - runs install.ps1 in this folder (all messages are in install.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
