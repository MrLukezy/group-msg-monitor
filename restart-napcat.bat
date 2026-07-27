@echo off
chcp 65001 >nul
cd /d "%~dp0third_party\NapCatShell"

echo [1/3] Stopping existing QQ / NapCat...
taskkill /F /IM NapCatWinBootMain.exe >nul 2>&1
taskkill /F /IM QQ.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/3] Starting NapCat Shell (launcher-user)...
echo After boot, look for WebUI URL in this window, e.g.:
echo   http://127.0.0.1:6099/webui?token=...
echo WebUI password/token is also in config\webui.json
echo.

call launcher-user.bat %*
