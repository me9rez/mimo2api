# mimo-bridge.ps1 — MiMo Desktop -> OpenAI 兼容反代（Windows 启动器 + 计划任务注册器）
#
# 用法:
#   powershell -NoProfile -File mimo-bridge.ps1              # 前台启动（可见输出）
#   powershell -NoProfile -File mimo-bridge.ps1 -Hidden      # 隐藏启动，输出追加到 bridge.log（计划任务用）
#   powershell -NoProfile -File mimo-bridge.ps1 -Install     # 注册计划任务（登录自启 + 崩溃自愈 + 隐藏窗口）
#   powershell -NoProfile -File mimo-bridge.ps1 -Status      # 查看端口 / 任务 / 日志状态
#   powershell -NoProfile -File mimo-bridge.ps1 -Uninstall   # 删除计划任务
#
# 关键设计（踩过的坑）:
#   - 计划任务的动作必须"一直等到子进程结束"（PowerShell 天然如此），否则任务立刻被判定
#     完成，RestartCount/RestartInterval 永远不会触发。
#   - 端口已被占用时直接退出，避免双实例；但这也意味着改完配置要真的杀掉旧进程再重启。
#   - 凭证优先级（mimo_bridge.py）: 环境变量 MIMO_PASS_TOKEN > 同目录 mimo_pass_token.json > Desktop cookie 库。
#     Windows 上 Desktop 运行时独占锁定 cookie 库，所以没有 json 时必须先退出 Desktop。
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Hidden,
    [switch]$Status,
    [string]$ApiKey = 'no-key-required',
    [int]$Port = 4500
)

$ErrorActionPreference = 'Stop'
$TaskName = 'mimo2api-bridge'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bridge = Join-Path $Dir 'mimo_bridge.py'
$Log = Join-Path $Dir 'bridge.log'

function Resolve-Python {
    # 本机唯一可用的是 Hermes 自带的 python，其次才退回 PATH
    $cand = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'hermes\tools') -Filter 'python-*' -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'python.exe' } |
            Where-Object { Test-Path $_ } |
            Sort-Object -Descending |
            Select-Object -First 1
    if ($cand) { return $cand }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'python not found: 装一个 python 或把 python.exe 放进 PATH'
}

function Test-BridgePort {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(700)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch { return $false } finally { $c.Close() }
}

function Install-Task {
    $ps1 = $MyInvocation.MyCommand.Path
    if (-not $ps1) { $ps1 = Join-Path $Dir 'mimo-bridge.ps1' }
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Hidden -Port {1}' -f $ps1, $Port) `
        -WorkingDirectory $Dir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description 'MiMo Desktop -> OpenAI compatible local bridge (127.0.0.1:4500)' -Force | Out-Null
    Write-Host "[ok] task '$TaskName' registered (AtLogOn, hidden, restart x3/1min, no time limit)"
    Write-Host "     立即启动: schtasks /run /tn $TaskName"
}

function Uninstall-Task {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[ok] task '$TaskName' removed"
}

function Show-Status {
    $listening = Test-BridgePort
    Write-Host ("port 127.0.0.1:{0} : {1}" -f $Port, $(if ($listening) { 'LISTENING' } else { 'closed' }))
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        $i = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ("task {0} : {1} / last result {2} / last run {3}" -f $TaskName, $t.State, $i.LastTaskResult, $i.LastRunTime)
    } else {
        Write-Host "task $TaskName : not registered (run -Install)"
    }
    if (Test-Path $Log) { Write-Host "--- bridge.log (tail) ---"; Get-Content $Log -Tail 6 }
}

function Start-Bridge {
    if (-not (Test-Path $Bridge)) { throw "missing $Bridge" }
    if (Test-BridgePort) {
        Write-Host "[i] 127.0.0.1:$Port already listening - bridge already up, nothing to do."
        exit 0
    }
    if (-not (Test-Path (Join-Path $Dir 'mimo_pass_token.json')) -and -not $env:MIMO_PASS_TOKEN) {
        Write-Host '[i] mimo_pass_token.json 不存在: 会去读 Desktop cookie 库，必须先完全退出 MiMo Desktop'
        Write-Host '[i]   （或先跑: python dump_windows_token.py）'
    }
    $py = Resolve-Python
    $env:API_KEY = $ApiKey
    $env:PORT = "$Port"
    if ($Hidden) {
        # PS 5.1 的 *>> 会把原生程序输出写成 UTF-16（日志里全是 \0），所以交给 cmd 做字节级重定向
        $line = '"{0}" -u "{1}" 1>>"{2}" 2>&1' -f $py, $Bridge, $Log
        & cmd.exe /c $line
    } else {
        Write-Host "[i] base_url = http://127.0.0.1:$Port/v1   api key = $ApiKey"
        Write-Host "[i] python   = $py"
        & $py -u $Bridge
    }
    exit $LASTEXITCODE
}

if ($Install)   { Install-Task;   exit 0 }
if ($Uninstall) { Uninstall-Task; exit 0 }
if ($Status)    { Show-Status;    exit 0 }
Start-Bridge
