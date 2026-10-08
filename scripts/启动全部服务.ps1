# -*- coding: utf-8 -*-
<#
    启动全部依赖服务（视频知识库用）

    做四件事：
      1. 确保 Docker Desktop 在跑（没跑就启）
      2. 等 Dify 就绪
      3. 确保 Ollama 在跑（没跑就启）
      4. 启动入库服务，并等它就绪

    用法：
      .\启动全部服务.ps1              正常启动
      .\启动全部服务.ps1 -Quiet      静默（开机自启用，不弹窗）
      .\启动全部服务.ps1 -Status     只看状态
#>

param(
    [switch]$Quiet,
    [switch]$Status,
    [string]$DockerExe = "",
    [string]$OllamaExe = "",
    [string]$Python = ""
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---- 路径自动探测（不需要改这个文件）----
$Dir     = $PSScriptRoot
if (-not $Dir) { $Dir = (Get-Location).Path }
$LogDir  = Join-Path $Dir "_logs"
$LogFile = Join-Path $LogDir "启动服务.log"

# Docker Desktop：先看参数，再找常见安装位置
if (-not $DockerExe) {
    $cands = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe",
        "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
    )
    $DockerExe = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
}

# docker CLI：通常在 PATH 上
$DockerCli = (Get-Command docker -ErrorAction SilentlyContinue).Source

# Ollama：先看参数，再看 PATH，再看常见位置
if (-not $OllamaExe) {
    $oc = Get-Command ollama -ErrorAction SilentlyContinue
    if ($oc) { $OllamaExe = $oc.Source }
    elseif (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") {
        $OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    }
}

# python：参数 → PATH 上的 python → py 启动器
if (-not $Python) {
    $pc = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pc) { $pc = Get-Command py -ErrorAction SilentlyContinue }
    if ($pc) { $Python = $pc.Source }
}

$IngestPy   = Join-Path $Dir "ingest-service.py"
$IngestPid  = Join-Path $Dir "_ingest-service.pid"
$IngestLog  = Join-Path $Dir "_ingest-service.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    if (-not $Quiet) { Write-Host "  $msg" }
}

function Test-Port($port) {
    return (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -ne $null
}

function Wait-For($name, $port, $timeoutSec) {
    Log "等待 $name（端口 $port，最多 $timeoutSec 秒）..."
    $n = [Math]::Ceiling($timeoutSec / 4)
    for ($i = 1; $i -le $n; $i++) {
        Start-Sleep -Seconds 4
        if (Test-Port $port) {
            Log "  $name 就绪（第 $($i*4) 秒）"
            return $true
        }
    }
    Log "  ⚠️ $name 超时未就绪"
    return $false
}

# ==================== 只看状态 ====================
if ($Status) {
    if (-not $Quiet) { Write-Host ""; Write-Host "  服务状态" -ForegroundColor Cyan; Write-Host ("  " + "-" * 46) }
    $items = @(
        @{ n = "Docker Desktop";  p = $null; chk = { (Get-Process 'Docker Desktop' -EA SilentlyContinue) -ne $null } },
        @{ n = "Dify (80)";       p = 80;    chk = $null },
        @{ n = "Ollama (11434)";  p = 11434; chk = $null },
        @{ n = "抓取服务 (8080)";  p = 8080;  chk = $null },
        @{ n = "入库服务 (8090)";  p = 8090;  chk = $null }
    )
    foreach ($it in $items) {
        $ok = if ($it.chk) { & $it.chk } else { Test-Port $it.p }
        $mark = if ($ok) { "运行中" } else { "已停止" }
        $color = if ($ok) { "Green" } else { "Red" }
        if (-not $Quiet) { Write-Host ("  [{0}] {1}" -f $mark, $it.n) -ForegroundColor $color }
    }
    return
}

if (-not $Quiet) {
    Write-Host ""
    Write-Host ("=" * 56) -ForegroundColor Cyan
    Write-Host "  启动视频知识库依赖服务" -ForegroundColor Cyan
    Write-Host ("=" * 56) -ForegroundColor Cyan
    Write-Host ""
}

Log "=== 开始启动 ==="

# ==================== 1. Docker Desktop ====================
$dockerRunning = $false
try {
    $v = & $DockerCli info --format "{{.ServerVersion}}" 2>&1
    if ($v -match '^\d+\.') { $dockerRunning = $true }
} catch { }

if ($dockerRunning) {
    Log "Docker 已在运行"
} else {
    Log "Docker 未运行，启动 Docker Desktop..."
    if (Test-Path $DockerExe) {
        Start-Process -FilePath $DockerExe
        $ok = $false
        for ($i = 1; $i -le 30; $i++) {
            Start-Sleep -Seconds 6
            try {
                $v = & $DockerCli info --format "{{.ServerVersion}}" 2>&1
                if ($v -match '^\d+\.') { Log "  Docker 就绪（第 $($i*6) 秒）"; $ok = $true; break }
            } catch { }
        }
        if (-not $ok) { Log "  ⚠️ Docker 启动超时，后续步骤可能失败" }
    } else {
        Log "  ❌ 找不到 $DockerExe"
    }
}

# ==================== 2. Ollama ====================
if (Test-Port 11434) {
    Log "Ollama 已在运行"
} else {
    Log "Ollama 未运行，启动..."
    if (Test-Path $OllamaExe) {
        Start-Process -FilePath $OllamaExe -ArgumentList "serve" -WindowStyle Hidden
        [void](Wait-For "Ollama" 11434 60)
    } else {
        Log "  ❌ 找不到 $OllamaExe"
    }
}

# ==================== 3. Dify ====================
if (Test-Port 80) {
    Log "Dify 端口已在监听，验证接口..."
    $ok = $false
    for ($i = 1; $i -le 20; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost/console/api/setup" -TimeoutSec 5 -UseBasicParsing -EA Stop
            Log "  Dify 就绪（第 $($i*4) 秒）"
            $ok = $true; break
        } catch { Start-Sleep -Seconds 4 }
    }
    if (-not $ok) { Log "  ⚠️ Dify 接口无响应" }
} else {
    Log "⚠️ Dify 端口 80 未监听（容器可能还在起）"
    [void](Wait-For "Dify" 80 120)
}

# ==================== 4. 入库服务 ====================
$ingestUp = $false
if (Test-Path $IngestPid) {
    $oldPid = (Get-Content $IngestPid -Raw -EA SilentlyContinue).Trim()
    if ($oldPid -and (Get-Process -Id $oldPid -EA SilentlyContinue)) { $ingestUp = $true }
}
if (Test-Port 8090) { $ingestUp = $true }

if ($ingestUp) {
    Log "入库服务已在运行"
} else {
    Log "启动入库服务..."
    if (-not (Test-Path $IngestPy)) {
        Log "  ❌ 找不到 $IngestPy"
    } elseif (-not (Test-Path $Python)) {
        Log "  ❌ 找不到 Python: $Python"
    } else {
        $env:PYTHONIOENCODING = "utf-8"
        $proc = Start-Process -FilePath $Python `
            -ArgumentList @($IngestPy, "8090") `
            -WorkingDirectory $Dir `
            -RedirectStandardOutput $IngestLog `
            -RedirectStandardError "$IngestLog.err" `
            -PassThru -WindowStyle Hidden
        $proc.Id | Out-File -FilePath $IngestPid -Encoding ascii -NoNewline
        Log "  进程已起 (PID $($proc.Id))"

        $ok = $false
        for ($i = 1; $i -le 20; $i++) {
            Start-Sleep -Seconds 3
            try {
                $h = Invoke-RestMethod -Uri "http://127.0.0.1:8090/health" -TimeoutSec 6
                Log "  入库服务就绪（第 $($i*3) 秒）"
                $ok = $true
                foreach ($k in $h.checks.PSObject.Properties.Name) {
                    $c = $h.checks.$k
                    Log ("    [{0}] {1}" -f $(if ($c.ok) { "OK  " } else { "FAIL" }), $k)
                }
                break
            } catch {
                if ($proc.HasExited) { Log "  ❌ 进程退出，看 $IngestLog.err"; break }
            }
        }
        if (-not $ok) { Log "  ⚠️ 入库服务未就绪" }
    }
}

# ==================== 汇总 ====================
Log "=== 启动流程结束 ==="
if (-not $Quiet) {
    Write-Host ""
    Write-Host "  服务状态：" -ForegroundColor Cyan
    & $PSCommandPath -Status
    Write-Host ""
    Write-Host "  日志：$LogFile" -ForegroundColor DarkGray
    Write-Host ""
    Read-Host "  按回车退出"
}
