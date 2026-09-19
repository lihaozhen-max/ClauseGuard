#Requires -Version 5.1
<#
.SYNOPSIS
  查看 ClauseGuard 各服务的运行状态（端口 / 进程 / 健康检查）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/dev_status.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$rows = @()

function Get-ListenerInfo([int]$port) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) { return @{ Names = '-'; Pids = '-' } }
    $names = @()
    $pids = @()
    foreach ($conn in $connections) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) { $names += $proc.ProcessName; $pids += $proc.Id }
    }
    return @{
        Names = (($names | Select-Object -Unique) -join ',')
        Pids  = (($pids | Select-Object -Unique) -join ',')
    }
}

function Get-HealthText([string]$url, [string]$expect) {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        if ($expect -and $response.Content -notlike "*$expect*") { return "端口被占用（非本服务）" }
        return "OK (HTTP $($response.StatusCode))"
    } catch {
        return '不可达'
    }
}

$services = @(
    @{ Name = 'MySQL';           Port = 3306; Health = $null;                              Expect = $null },
    @{ Name = '模拟审批系统';    Port = 8100; Health = 'http://127.0.0.1:8100/health';     Expect = 'approvals' },
    @{ Name = '工具服务（后端）'; Port = 8000; Health = 'http://127.0.0.1:8000/health';    Expect = 'ClauseGuard' },
    @{ Name = '调用端（前端）';  Port = 5173; Health = 'http://127.0.0.1:5173/';          Expect = 'ClauseGuard' }
)

Write-Host ''
Write-Host ' ClauseGuard 服务状态' -ForegroundColor Cyan
Write-Host (' ' + ('-' * 78)) -ForegroundColor DarkGray
Write-Host (' {0,-18} {1,-6} {2,-22} {3,-16} {4}' -f '服务', '端口', '进程', 'PID', '健康检查') -ForegroundColor White
Write-Host (' ' + ('-' * 78)) -ForegroundColor DarkGray

foreach ($service in $services) {
    $info = Get-ListenerInfo $service.Port
    if ($service.Name -eq 'MySQL') {
        $status = & docker ps --filter 'name=clauseguard-mysql' --format '{{.Status}}' 2>$null
        $health = if ($status) { $status } else { '容器未运行' }
    } elseif ($service.Health) {
        $health = Get-HealthText $service.Health $service.Expect
    } else {
        $health = if ($info.Names -ne '-') { '端口占用' } else { '未监听' }
    }
    $color = if ($health -like 'OK*' -or $health -like '*healthy*') { 'Green' } else { 'Yellow' }
    Write-Host (' {0,-18} {1,-6} {2,-22} {3,-16} {4}' -f $service.Name, $service.Port, $info.Names, $info.Pids, $health) -ForegroundColor $color
}

Write-Host (' ' + ('-' * 78)) -ForegroundColor DarkGray

# 前端可能因 5173 被别的项目占用而落到备用端口（dev_up.ps1 会自动顺延），这里扫一遍报出来
$foundFrontend = @()
foreach ($port in 5173..5180) {
    $text = Get-HealthText "http://127.0.0.1:$port/" 'ClauseGuard'
    if ($text -like 'OK*') { $foundFrontend += "http://127.0.0.1:$port" }
}
if ($foundFrontend.Count -gt 0) {
    Write-Host (' 本项目调用端：' + ($foundFrontend -join '、')) -ForegroundColor Green
} else {
    Write-Host ' 本项目调用端：未在 5173–5180 上找到（可能没启动）' -ForegroundColor Yellow
}

Write-Host ' 一键启动 / 停止：scripts/dev_up.ps1 · scripts/dev_down.ps1' -ForegroundColor DarkGray
Write-Host ' 注意：端口 8000/3306 上还可能有 Docker 的 relay 进程（wslrelay），那是正常的。' -ForegroundColor DarkGray
Write-Host ''
