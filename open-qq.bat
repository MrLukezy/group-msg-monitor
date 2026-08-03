@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 日常打开 QQ 的统一入口：把桌面 QQ 快捷方式换成这个文件即可。
rem 已在运行则唤到前台，没运行才通过 NapCat 注入启动，避免出现「重复登录」提示。

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\show_qq_window.ps1" >nul 2>&1
set "PROBE=%errorlevel%"

if "%PROBE%"=="0" exit /b 0

if "%PROBE%"=="3" (
  echo QQ 正在后台运行，但主面板已被关闭。
  echo 请点击任务栏右下角托盘区的 QQ 图标恢复窗口。
  echo 若托盘里也找不到，关闭本窗口后重新运行本文件即可。
  pause
  exit /b 0
)

echo QQ 未在运行，正在通过 NapCat 启动（同时提供监听接口）...
call restart-napcat.bat %*
