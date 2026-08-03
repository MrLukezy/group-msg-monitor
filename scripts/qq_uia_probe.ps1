# QQ 被动采集：通过 UI Automation 读取官方 QQ 当前会话标题与可见文本。
# 输出一行 JSON。

$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8

function Emit($obj) {
    ($obj | ConvertTo-Json -Compress -Depth 6)
}

try {
    $qq = @(Get-Process -Name QQ -ErrorAction SilentlyContinue)
    if ($qq.Count -eq 0) {
        Emit @{ ok = $false; error = 'official QQ not running'; groupName = ''; messages = @() }
        exit 0
    }

    $main = $qq | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | Select-Object -First 1
    $groupName = ''
    if ($main) {
        $groupName = [string]$main.MainWindowTitle
        if ($groupName -match '^(.*?)\s*-\s*QQ\s*$') { $groupName = $Matches[1].Trim() }
    }

    $messages = New-Object System.Collections.ArrayList
    $uiaLoaded = $false
    try {
        Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop | Out-Null
        Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop | Out-Null
        $uiaLoaded = $true
    } catch {
        $uiaLoaded = $false
    }

    if ($uiaLoaded -and $main) {
        try {
            $target = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$main.MainWindowHandle)
            if ($target) {
                if (-not $groupName -and $target.Current.Name) {
                    $groupName = [string]$target.Current.Name
                    if ($groupName -match '^(.*?)\s*-\s*QQ\s*$') { $groupName = $Matches[1].Trim() }
                }
                $textCond = New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::Text)
                $nodes = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, $textCond)
                $seen = @{}
                $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                $count = 0
                foreach ($node in $nodes) {
                    if ($count -ge 40) { break }
                    try {
                        $name = ([string]$node.Current.Name).Trim()
                        if (-not $name) { continue }
                        if ($name.Length -lt 1 -or $name.Length -gt 500) { continue }
                        if ($name -eq 'QQ' -or $name -eq $groupName) { continue }
                        if ($seen.ContainsKey($name)) { continue }
                        $seen[$name] = $true
                        $sender = ''
                        $text = $name
                        if ($name -match '^\s*([^:：\n]{1,32})\s*[:：]\s*(.+)\s*$') {
                            $sender = $Matches[1].Trim()
                            $text = $Matches[2].Trim()
                        }
                        [void]$messages.Add(@{
                            sender = $sender
                            text = $text
                            observedAt = [double]$now
                        })
                        $count++
                    } catch { continue }
                }
            }
        } catch {
            # keep title-only
        }
    }

    Emit @{
        ok = [bool]$groupName -or ($messages.Count -gt 0)
        groupName = $groupName
        messages = @($messages)
        count = $messages.Count
        error = if ($groupName -or $messages.Count -gt 0) { '' } else { 'no visible QQ window title' }
    }
}
catch {
    Emit @{ ok = $false; error = $_.Exception.Message; groupName = ''; messages = @() }
}
