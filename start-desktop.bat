@echo off
chcp 65001 >nul
cd /d "%~dp0desktop"
if not exist "node_modules\" (
  echo Installing npm packages...
  call npm install
)
echo Starting Tauri desktop UI...
call npm run tauri dev
