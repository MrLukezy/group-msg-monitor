# 读取 Windows 通知中心中的 QQ Toast（只读，不清除）。
# 输出一行 JSON。无包身份时可能返回 access=unsupported。

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8

function Emit($obj) {
    $obj | ConvertTo-Json -Compress -Depth 6
}

function Await-WinRt($op) {
    if ($null -eq $op) { return $null }
    while ($op.Status -eq 0) { Start-Sleep -Milliseconds 40 }
    if ($op.Status -ne 1) {
        throw "WinRT async failed status=$($op.Status)"
    }
    if ($op.PSObject.Methods.Name -contains 'GetResults') {
        return $op.GetResults()
    }
    return $null
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction SilentlyContinue | Out-Null
    $null = [Windows.UI.Notifications.Management.UserNotificationListener, Windows.UI.Notifications.Management, ContentType = WindowsRuntime]
    $listener = [Windows.UI.Notifications.Management.UserNotificationListener]::Current

    $accessName = 'unsupported'
    try {
        $accessOp = $listener.RequestAccessAsync()
        $access = Await-WinRt $accessOp
        $accessName = switch ([int]$access) {
            1 { 'allowed' }
            2 { 'denied' }
            3 { 'unspecified' }
            default { 'default' }
        }
    } catch {
        Emit @{
            ok = $false
            access = 'unsupported'
            error = $_.Exception.Message
            items = @()
            hint = '当前进程缺少通知监听包身份；被动模式将主要依赖 UIA 当前会话补偿'
        }
        exit 0
    }

    if ($accessName -ne 'allowed') {
        Emit @{ ok = $false; access = $accessName; error = "通知访问未授权($accessName)"; items = @() }
        exit 0
    }

    $getOp = $listener.GetNotificationsAsync([Windows.UI.Notifications.Management.NotificationKinds]::Toast)
    $notifs = Await-WinRt $getOp
    $items = New-Object System.Collections.ArrayList
    foreach ($n in $notifs) {
        try {
            $appInfo = $n.AppInfo
            $appId = ''
            $appName = ''
            if ($appInfo) {
                $appId = [string]$appInfo.AppUserModelId
                try { $appName = [string]$appInfo.DisplayInfo.DisplayName } catch { $appName = '' }
            }
            $blob = ("$appId $appName").ToLowerInvariant()
            if ($blob -notmatch 'qq|tencent') { continue }

            $title = ''
            $bodyParts = New-Object System.Collections.ArrayList
            $toast = $n.Notification
            if ($toast -and $toast.Visual) {
                $binding = $toast.Visual.GetBinding([Windows.UI.Notifications.KnownNotificationBindings]::ToastGeneric)
                if ($binding) {
                    foreach ($text in $binding.GetTextElements()) {
                        $val = [string]$text.Text
                        if (-not $val) { continue }
                        if (-not $title) { $title = $val } else { [void]$bodyParts.Add($val) }
                    }
                }
            }
            $created = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            try { $created = [double]$n.CreationTime.ToUnixTimeSeconds() } catch {}
            [void]$items.Add(@{
                id = [string]$n.Id
                appId = $appId
                appName = $appName
                title = $title
                body = ($bodyParts -join "`n")
                createdAt = $created
            })
        } catch { continue }
    }
    Emit @{ ok = $true; access = 'allowed'; items = @($items); count = $items.Count }
}
catch {
    Emit @{
        ok = $false
        access = 'unsupported'
        error = $_.Exception.Message
        items = @()
        hint = '通知监听不可用；被动模式仍可通过 UIA 采集当前打开会话'
    }
}
