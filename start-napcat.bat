@echo off
chcp 65001 >nul
cd /d "%~dp0third_party\NapCatShell"
echo Starting NapCat Shell (needs Administrator + QQ login)...
echo After start, open WebUI from console URL (usually http://127.0.0.1:6099/webui)
echo WS for monitor: ws://127.0.0.1:3001  (token already synced with project .env)
call launcher.bat %*
