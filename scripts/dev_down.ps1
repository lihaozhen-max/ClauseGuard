#Requires -Version 5.1
<#
.SYNOPSIS
  停止 ClauseGuard 由本仓库启动的服务（模拟审批系统 / 工具服务 / 调用端）。

.DESCRIPTION
  **只杀属于本项目的 python / node 进程**，并按端口精确定位；
  不会碰 Docker（它也会监听 8000/3306）与你的浏览器。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/dev_down.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$targets = @(
    @{ Port = 5173; Label = '调用端（Vite）' },
    @{ Port = 8000; Label = '工具服务（后端）' },
    @{ Port = 8100; Label = '模拟审批系统' }
)
$ownNames = @('python', 'pythonw', 'node')

$stopped = 0
foreach ($target in $targets) {
    $connections = Get-NetTCPConnection -LocalPort $target.Port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) {
        Write-Host "     [--] $($target.Label)（端口 $($target.Port)）本来就没在跑" -ForegroundColor DarkGray
        continue
    }
    $hit = $false
    foreach ($conn in $connections) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if ($ownNames -notcontains $proc.ProcessName) {
            Write-Host "     [跳过] 端口 $($target.Port) 上是 $($proc.ProcessName)（不属于本项目，如 Docker relay）" -ForegroundColor Yellow
            continue
        }
        $hit = $true
        Write-Host "     [停止] $($target.Label)：$($proc.ProcessName) (pid $($proc.Id))" -ForegroundColor Green
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $stopped++
    }
    if (-not $hit) {
        Write-Host "     [--] $($target.Label) 未找到本项目的进程" -ForegroundColor DarkGray
    }
}

Start-Sleep -Seconds 2
Write-Host ''
Write-Host "已停止 $stopped 个进程。MySQL 容器仍在运行；要一并停掉：" -ForegroundColor Cyan
Write-Host "  cd database; docker compose --env-file ../.env down" -ForegroundColor DarkGray
