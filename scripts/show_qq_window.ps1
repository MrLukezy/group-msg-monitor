# 探测并唤起 NapCat 注入的 QQ 主窗口。
# 该 QQ 与监听共用同一登录会话，日常聊天必须用它；另开官方 QQ 只会得到「重复登录」提示。
#
# 输出约定（供 open-qq.bat 与 Tauri 后端判断）：
#   activated:<pid>  已把现有 QQ 窗口唤到前台
#   tray-only        QQ 进程在跑但主面板已销毁，只能从托盘图标恢复
#   not-running      没有 QQ 进程，需要通过 NapCat 启动

$signature = @'
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
'@

$win32 = Add-Type -MemberDefinition $signature -Name QqWindow -Namespace Native -PassThru

$processes = @(Get-Process -Name QQ -ErrorAction SilentlyContinue)
if ($processes.Count -eq 0) {
    Write-Output 'not-running'
    exit 2
}

$target = $processes | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $target) {
    Write-Output 'tray-only'
    exit 3
}

# SW_RESTORE：从最小化状态恢复并置前
$win32::ShowWindow($target.MainWindowHandle, 9) | Out-Null
$win32::SetForegroundWindow($target.MainWindowHandle) | Out-Null
Write-Output "activated:$($target.Id)"
exit 0
