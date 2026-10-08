# -*- coding: utf-8 -*-
<#
    启动 / 停止 / 查看「视频入库服务」

    用法：
      .\入库服务.ps1                启动
      .\入库服务.ps1 status         看状态
      .\入库服务.ps1 stop           停止
      .\入库服务.ps1 restart        重启

    ⭐ 全部路径自动探测（脚本所在目录 + PATH 上的 python），不需要改这个文件。
       如果 python 不在 PATH 上：
         .\入库服务.ps1 -Python "C:\Python312\python.exe"
       想换端口：
         .\入库服务.ps1 -Port 8091
#>

param(
    [ValidateSet('start','stop','status','restart')]
    [string]$Action = 'start',
    [int]$Port = 8090,
    [string]$Python = ""
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---- 路径自动探测 ----
$ServiceDir = $PSScriptRoot
if (-not $ServiceDir) { $ServiceDir = (Get-Location).Path }
$Script  = Join-Path $ServiceDir "ingest-service.py"
$PidFile = Join-Path $ServiceDir "_ingest-service.pid"
$LogFile = Join-Path $ServiceDir "_ingest-service.log"

# python 探测：参数 → PATH 上的 python → py 启动器
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command py -ErrorAction SilentlyContinue }
    if ($cmd) { $Python = $cmd.Source }
}

function Title($m) {
    Write-Host ""
    Write-Host ("=" * 56) -ForegroundColor Cyan
    Write-Host "  $m" -ForegroundColor Cyan
    Write-Host ("=" * 56) -ForegroundColor Cyan
}

function Get-Running {
    if (-not (Test-Path $PidFile)) { return $null }
    $oldPid = (Get-Content $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if (-not $oldPid) { return $null }
    $p = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($p) { return $p }
    return $null
}

function Test-Health {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 8 }
    catch { return $null }
}

switch ($Action) {

    'status' {
        Title "入库服务状态"
        $p = Get-Running
        if ($p) { Write-Host "  进程: 运行中 (PID $($p.Id))" -ForegroundColor Green }
        else    { Write-Host "  进程: 未运行" -ForegroundColor Yellow }

        $h = Test-Health
        if ($h) {
            Write-Host "  接口: 正常 (http://127.0.0.1:$Port)" -ForegroundColor Green
            foreach ($k in $h.checks.PSObject.Properties.Name) {
                $c = $h.checks.$k
                $color = if ($c.ok) { "Green" } else { "Red" }
                $mark  = if ($c.ok) { "OK  " } else { "FAIL" }
                $extra = ""
                if ($k -eq 'ollama' -and $c.models) { $extra = "  " + ($c.models -join ', ') }
                elseif (-not $c.ok) { $extra = "  " + $c.error }
                Write-Host "        [$mark] $k$extra" -ForegroundColor $color
            }
        } else {
            Write-Host "  接口: 无响应" -ForegroundColor Red
        }
    }

    'stop' {
        Title "停止入库服务"
        $p = Get-Running
        if (-not $p) { Write-Host "  本来就没在跑" -ForegroundColor Yellow; break }
        Stop-Process -Id $p.Id -Force
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "  已停止 (PID $($p.Id))" -ForegroundColor Green
    }

    'restart' {
        & $PSCommandPath stop -Port $Port -Python $Python
        Start-Sleep -Seconds 2
        & $PSCommandPath start -Port $Port -Python $Python
    }

    'start' {
        Title "启动入库服务"

        $p = Get-Running
        if ($p) {
            Write-Host "  已在运行 (PID $($p.Id))，不重复启动" -ForegroundColor Yellow
            if (Test-Health) { Write-Host "  接口正常" -ForegroundColor Green }
            break
        }

        if (-not (Test-Path $Script)) {
            Write-Host "  ❌ 找不到 $Script" -ForegroundColor Red
            Write-Host "     这个脚本要和 ingest-service.py 放在同一个目录" -ForegroundColor Yellow
            Read-Host "  按回车退出"; exit 1
        }
        if (-not $Python -or -not (Test-Path $Python)) {
            Write-Host "  ❌ 找不到 python" -ForegroundColor Red
            Write-Host "     装一个，或者用 -Python 指定路径：" -ForegroundColor Yellow
            Write-Host "       .\入库服务.ps1 -Python `"C:\Python312\python.exe`"" -ForegroundColor Yellow
            Read-Host "  按回车退出"; exit 1
        }

        Write-Host "  python : $Python"
        Write-Host "  脚本   : $Script"
        Write-Host "  端口   : $Port"
        Write-Host "  启动中..."

        $env:PYTHONIOENCODING = "utf-8"
        $proc = Start-Process -FilePath $Python `
            -ArgumentList @($Script, "$Port") `
            -WorkingDirectory $ServiceDir `
            -RedirectStandardOutput $LogFile `
            -RedirectStandardError "$LogFile.err" `
            -PassThru -WindowStyle Hidden

        $proc.Id | Out-File -FilePath $PidFile -Encoding ascii -NoNewline
        Write-Host "  进程已起 (PID $($proc.Id))，等它就绪..." -ForegroundColor Green

        $ok = $false
        for ($i = 1; $i -le 15; $i++) {
            Start-Sleep -Seconds 2
            $h = Test-Health
            if ($h) {
                Write-Host ""
                Write-Host "  ✅ 第 $($i*2) 秒就绪  http://127.0.0.1:$Port" -ForegroundColor Green
                Write-Host ""
                foreach ($k in $h.checks.PSObject.Properties.Name) {
                    $c = $h.checks.$k
                    $color = if ($c.ok) { "Green" } else { "Red" }
                    $mark  = if ($c.ok) { "OK  " } else { "FAIL" }
                    $extra = ""
                    if ($k -eq 'ollama' -and $c.models) { $extra = "  " + ($c.models -join ', ') }
                    elseif (-not $c.ok) { $extra = "  " + $c.error }
                    Write-Host "        [$mark] $k$extra" -ForegroundColor $color
                }
                $ok = $true
                break
            }
            if ($proc.HasExited) {
                Write-Host "  ❌ 进程退出了，看日志：$LogFile.err" -ForegroundColor Red
                Get-Content "$LogFile.err" -Tail 20 -ErrorAction SilentlyContinue |
                    ForEach-Object { Write-Host "     $_" }
                break
            }
        }
        if (-not $ok -and -not $proc.HasExited) {
            Write-Host "  ⚠️ 30 秒没就绪，检查日志：$LogFile" -ForegroundColor Yellow
        }

        Write-Host ""
        Write-Host "  ────────────────────────────────" -ForegroundColor DarkGray
        Write-Host "    查询任务:  curl http://127.0.0.1:$Port/jobs" -ForegroundColor DarkGray
        Write-Host "    健康检查:  curl http://127.0.0.1:$Port/health" -ForegroundColor DarkGray
        Write-Host "    停止服务:  .\入库服务.ps1 stop" -ForegroundColor DarkGray
        Write-Host ""
    }
}

if ($Action -eq 'start') { Read-Host "按回车退出" }
