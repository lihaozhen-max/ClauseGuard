#Requires -Version 5.1
<#
.SYNOPSIS
  一键启动 ClauseGuard 的全部服务（MySQL → 模拟审批系统 → 工具服务 → 调用端）。

.DESCRIPTION
  - 幂等：已在跑的服务会跳过，不重复占端口；
  - 用 WMI（Win32_Process.Create）启动**真正脱离调用方**的进程：
    关掉当前终端 / 会话结束时服务不会被连带杀掉（Start-Process 做不到这点）；
  - 日志写到 ClauseGuard/_logs/（已被 .gitignore 忽略）；
  - 文件必须保存为 UTF-8 with BOM —— Windows PowerShell 5.1 会按 ANSI 解码无 BOM 的 .ps1，
    中文会被打乱甚至破坏语法。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/dev_up.ps1
  powershell -ExecutionPolicy Bypass -File scripts/dev_up.ps1 -NoFrontend   # 只起后端
#>
[CmdletBinding()]
param(
    [switch]$NoFrontend
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root '_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$mysqlContainer = 'clauseguard-mysql'
$servicePort = 8000
$mockPort = 8100
$webPort = 5173


function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    [OK] $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    [!]  $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "    [X]  $text" -ForegroundColor Red }

function Get-Health([string]$url) {
    try { return Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 } catch { return $null }
}

function Test-Listening([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

<#
  启动一个脱离当前会话的进程。

  关键点：**不用 Start-Process** —— 那样起的子进程仍属于当前进程树，
  调用方（终端 / CI / agent 会话）一结束就被连带杀掉，表现为"服务莫名消失"。
  Win32_Process.Create 由 WMI 提供程序托管，进程因此真正独立。
#>
function Start-Detached([string]$workDir, [string]$command, [string]$logFile) {
    $line = 'cmd.exe /c cd /d "{0}" && {1} > "{2}" 2>&1' -f $workDir, $command, $logFile
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $line }
    if ($result.ReturnValue -ne 0) {
        throw "创建进程失败（Win32_Process.Create 返回 $($result.ReturnValue)）：$command"
    }
    return [int]$result.ProcessId
}

function Wait-Health([string]$label, [string]$url, [string]$expect, [int]$seconds, [string]$logFile) {
    $started = Get-Date
    for ($i = 0; $i -lt $seconds; $i++) {
        Start-Sleep -Seconds 1
        $response = Get-Health $url
        if ($response -and (-not $expect -or $response.Content -like "*$expect*")) {
            $cost = [int]((Get-Date) - $started).TotalSeconds
            Write-Host "    （等待 ${cost}s 后就绪）" -ForegroundColor DarkGray
            return $true
        }
        if ($i -gt 0 -and $i % 15 -eq 0) {
            Write-Host "    仍在等待 $label … 已 $i s（首次启动要导入 Paddle 等重依赖，1–2 分钟属正常）" -ForegroundColor DarkGray
        }
    }
    if (Test-Path $logFile) {
        Write-Host "    ---- $([System.IO.Path]::GetFileName($logFile)) 末尾 ----" -ForegroundColor DarkGray
        Get-Content $logFile -Tail 12 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
    return $false
}

# ── 1) MySQL ───────────────────────────────────────────────────────────────
Write-Step "MySQL 容器（$mysqlContainer，端口 3306）"
$status = & docker ps --filter "name=$mysqlContainer" --format '{{.Status}}' 2>$null
if ($status -and $status -like '*Up*') {
    Write-Ok "已在运行：$status"
} else {
    Write-Warn "未运行，正在启动 docker compose…"
    Push-Location (Join-Path $root 'database')
    try {
        & docker compose --env-file ../.env up -d
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Seconds 2
            $status = & docker ps --filter "name=$mysqlContainer" --format '{{.Status}}' 2>$null
            if ($status -like '*healthy*') { break }
        }
    } finally { Pop-Location }
    if ($status -like '*healthy*') { Write-Ok "已就绪：$status" }
    else { Write-Bad "MySQL 未就绪（当前：$status）。检查 database/docker-compose.yml 与 .env 中的口令" }
}

# ── 2) 模拟审批系统 ────────────────────────────────────────────────────────
Write-Step "模拟审批系统（端口 $mockPort）"
if (Get-Health "http://127.0.0.1:$mockPort/health") {
    Write-Ok "已在运行"
} else {
    if (Test-Listening $mockPort) { Write-Warn "端口被非本服务的进程占用，仍尝试启动" }
    $mockPid = Start-Detached $root `
        "uv run --project backend uvicorn --app-dir mock-approval-system main:app --host 127.0.0.1 --port $mockPort --log-level warning" `
        (Join-Path $logDir 'mock.log')
    if (Wait-Health '模拟审批系统' "http://127.0.0.1:$mockPort/health" 'approvals' 90 (Join-Path $logDir 'mock.log')) {
        Write-Ok "已启动（pid $mockPid）：http://127.0.0.1:$mockPort"
    } else {
        Write-Bad "启动失败，见 _logs/mock.log"
    }
}

# ── 3) 工具服务（后端）─────────────────────────────────────────────────────
Write-Step "工具服务 / 后端（端口 $servicePort）"
$current = Get-Health "http://127.0.0.1:$servicePort/health"
if ($current -and $current.Content -like '*ClauseGuard*') {
    Write-Ok "已在运行"
} else {
    if (Test-Listening $servicePort) {
        Write-Warn "端口 $servicePort 上已有监听（通常是 Docker 的 wslrelay，只占 IPv6）；本项目绑定 IPv4 127.0.0.1"
    }
    $backendPid = Start-Detached (Join-Path $root 'backend') `
        "uv run uvicorn app.main:app --host 127.0.0.1 --port $servicePort --log-level warning" `
        (Join-Path $logDir 'backend.log')
    if (Wait-Health '工具服务' "http://127.0.0.1:$servicePort/health" 'ClauseGuard' 240 (Join-Path $logDir 'backend.log')) {
        $payload = (Get-Health "http://127.0.0.1:$servicePort/health").Content | ConvertFrom-Json
        Write-Ok "已启动（pid $backendPid）：数据库 connected=$($payload.database.connected)，LLM=$($payload.llm_enabled)"
    } else {
        # 用词很重要：进程已经拉起来了，只是健康检查还没通过——首次冷启动要导入 Paddle 等重依赖，
        # 实测可以超过 4 分钟（机器忙时更久）。不要把这种情况报成"启动失败"，那会误导人。
        Write-Warn "已发起启动，但健康检查 240 秒内未通过。请稍后用 scripts/dev_status.ps1 再确认；"
        Write-Warn "若仍未就绪，再看 _logs/backend.log（常见原因：.env 缺必填项，服务 fail-fast）"
    }
}

# ── 4) 调用端（前端）───────────────────────────────────────────────────────
if (-not $NoFrontend) {
    Write-Step "调用端 / 前端（端口 $webPort）"
    if (Get-Health "http://127.0.0.1:$webPort/") {
        Write-Ok "已在运行"
    } else {
        $webRoot = Join-Path $root 'frontend-or-client'
        if (-not (Test-Path (Join-Path $webRoot 'node_modules'))) {
            Write-Warn "node_modules 不存在，先执行 npm install（首次约 1–2 分钟，请稍等）"
            Push-Location $webRoot
            try { & cmd.exe /c 'npm install' | Out-Null } finally { Pop-Location }
        }
        $webPid = Start-Detached $webRoot 'npm run dev' (Join-Path $logDir 'web.log')
        if (Wait-Health '调用端' "http://127.0.0.1:$webPort/" $null 120 (Join-Path $logDir 'web.log')) {
            Write-Ok "已启动（pid $webPid）：http://127.0.0.1:$webPort"
        } else {
            Write-Bad "启动失败，见 _logs/web.log"
        }
    }
}

Write-Host ''
Write-Host '----------------------------------------------------------' -ForegroundColor DarkGray
Write-Host ' 调用端      http://127.0.0.1:5173' -ForegroundColor White
Write-Host ' 工具服务    http://127.0.0.1:8000/health' -ForegroundColor White
Write-Host ' 审批模拟    http://127.0.0.1:8100/health' -ForegroundColor White
Write-Host ' 停止全部    powershell -File scripts/dev_down.ps1' -ForegroundColor DarkGray
Write-Host ' 查看状态    powershell -File scripts/dev_status.ps1' -ForegroundColor DarkGray
Write-Host ' 日志目录    _logs/' -ForegroundColor DarkGray
Write-Host '----------------------------------------------------------' -ForegroundColor DarkGray
