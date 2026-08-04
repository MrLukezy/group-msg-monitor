@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] Starting Docker Desktop...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
echo Please wait until the whale icon is steady in the tray.
echo.

echo [2/3] Waiting for Docker engine...
set DOCKER="C:\Program Files\Docker\Docker\resources\bin\docker.exe"
set /a n=0
:wait
set /a n+=1
%DOCKER% info >nul 2>nul
if %ERRORLEVEL%==0 goto ready
if %n% GEQ 60 (
  echo Docker engine not ready after ~5 minutes. Open Docker Desktop manually and re-run this script.
  pause
  exit /b 2
)
timeout /t 5 /nobreak >nul
goto wait

:ready
echo Docker engine is ready.
echo.
echo [3/3] Starting GeWeChat container...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-gewechat.ps1"
if errorlevel 1 (
  echo GeWeChat start failed.
  pause
  exit /b 1
)
echo.
echo Done. In the desktop app: Channels -^> WeChat GeWeChat -^> Scan login.
pause
