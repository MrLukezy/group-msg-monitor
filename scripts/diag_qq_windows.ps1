# 诊断用：枚举所有 QQ 进程的顶层窗口，输出可见性、尺寸、类名与标题。
$signature = @'
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
[DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr hWnd, System.Text.StringBuilder s, int max);
[DllImport("user32.dll")] public static extern int GetClassNameW(IntPtr hWnd, System.Text.StringBuilder s, int max);
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
public struct RECT { public int Left, Top, Right, Bottom; }
'@

# -PassThru 会连带返回委托与结构体类型，需挑出宿主类本身
$w = @(Add-Type -MemberDefinition $signature -Name Diag -Namespace QqDiag -PassThru -UsingNamespace System.Text) |
    Where-Object { $_.Name -eq 'Diag' } | Select-Object -First 1

$qqPids = @(Get-Process -Name QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
if ($qqPids.Count -eq 0) { Write-Output 'no QQ process'; exit 1 }

$rows = New-Object System.Collections.ArrayList
$callback = [QqDiag.Diag+EnumWindowsProc] {
    param($hWnd, $lParam)
    # 不能用 $pid：那是 PowerShell 的自动变量，赋值会被忽略导致过滤失效
    $ownerPid = 0
    [void]$w::GetWindowThreadProcessId($hWnd, [ref]$ownerPid)
    if ($qqPids -notcontains $ownerPid) { return $true }

    $title = New-Object System.Text.StringBuilder 512
    [void]$w::GetWindowTextW($hWnd, $title, 512)
    $cls = New-Object System.Text.StringBuilder 512
    [void]$w::GetClassNameW($hWnd, $cls, 512)
    $rect = New-Object QqDiag.Diag+RECT
    [void]$w::GetWindowRect($hWnd, [ref]$rect)

    [void]$rows.Add([pscustomobject]@{
        Pid     = $ownerPid
        Handle  = $hWnd
        Visible = $w::IsWindowVisible($hWnd)
        Size    = "$($rect.Right - $rect.Left)x$($rect.Bottom - $rect.Top)"
        Pos     = "$($rect.Left),$($rect.Top)"
        Class   = $cls.ToString()
        Title   = $title.ToString()
    })
    return $true
}

[void]$w::EnumWindows($callback, [IntPtr]::Zero)
$rows | Sort-Object -Property Visible -Descending | Format-Table -AutoSize | Out-String -Width 200
