<#
  一键启动整套直播流程（弹幕抓取 + 语音转录 + 决策/前端）。

  用法：
    # 影子模式：生成候选但不发送（默认）
    powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -RoomId 12345678

    # 自动回复：通过护栏后真的发弹幕（队列限速 ≥3 秒、≤8 条/分）
    powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -RoomId 12345678 -Send

    # 只看数据、连模型都不跑
    powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -RoomId 12345678 -MonitorOnly

    # 纯监控，但仍然维护知识库（事实抽取/摘要/索引/人物卡）
    powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -RoomId 12345678 -MonitorOnly -Kb

    # 只重启转录进程（改 ASR 参数/代码后用；弹幕与前端保持运行）
    powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -RoomId 12345678 -AsrOnly

    # 停掉本脚本启动的三个进程
    powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -Stop

  产物：
    run\danmaku.log / .err   弹幕抓取的连接与状态
    run\asr.log / .err       语音转录（房间未开播时会等待）
    run\web.log / .err       决策服务与前端
    run\live_pids.txt        本次启动的 PID，供 -Stop 使用
#>
param(
    [int]$RoomId = 0,
    [int]$Port = 8765,
    [switch]$Send,
    [switch]$MonitorOnly,
    # 纯监控模式下仍跑知识库维护（默认关：监控模式不调用模型）
    [switch]$Kb,
    [switch]$Stop,
    [switch]$NoAsr,
    # 只重启决策/前端进程（采集与转录不动）——切换影子/发送模式时用，
    # 免得为了改一个开关把弹幕连接和转录流水都打断
    [switch]$WebOnly,
    # 只重启转录进程（弹幕与前端不动）——换了 ASR 参数/代码后用它
    [switch]$AsrOnly,
    [int]$WaitLiveInterval = 30,
    [string]$PythonExe = 'python',
    # 弹幕抓取必须带登录态：未登录时 B 站不投放 uid/昵称（全 0 + 打码），
    # 机器人就 @ 不了人、也存不了个人记忆（踩过一次，见 bug 记录）
    [string]$CookieFile = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root    = $PSScriptRoot
$runDir  = Join-Path $root 'run'
$pidFile = Join-Path $runDir 'live_pids.txt'
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

# 读取上次启动记录的进程（role=pid；旧格式是不带标签的纯数字）
$roles = @{}
if (Test-Path -LiteralPath $pidFile) {
    $i = 0
    foreach ($line in Get-Content -LiteralPath $pidFile) {
        $t = $line.Trim()
        if (-not $t) { continue }
        $i++
        if ($t.Contains('=')) {
            $kv = $t -split '=', 2
            $roles[$kv[0].Trim()] = [int]$kv[1].Trim()
        } else {
            $order = @('danmaku', 'asr', 'web')
            $roles[$order[[Math]::Min($i - 1, 2)]] = [int]$t
        }
    }
}

function Stop-All {
    if (-not (Test-Path $pidFile)) {
        Write-Host '没有记录在案的进程（run\live_pids.txt 不存在）。'
        return
    }
    foreach ($line in Get-Content $pidFile) {
        $t = $line.Trim()
        if (-not $t) { continue }
        # pid 文件格式：`role=pid`（早期版本是不带标签的纯数字，兼容处理）
        $num = if ($t.Contains('=')) { ($t -split '=')[-1] } else { $t }
        $procId = 0
        if (-not [int]::TryParse($num.Trim(), [ref]$procId)) { continue }
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($p) {
            Write-Host ("停止 PID {0} ({1})" -f $procId, $p.ProcessName)
            Stop-Process -Id $procId -Force
        }
    }
    Remove-Item -LiteralPath $pidFile -Force

    # 清掉遗留的 ffmpeg 子进程：强杀 Python 时它不会跟着退，会继续往切片目录写，
    # 下次启动就会撞上"文件被占用"（实测把 ASR 直接崩掉过）。
    try {
        $strays = Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" -ErrorAction Stop |
                  Where-Object { $_.CommandLine -like '*asr_chunks*' }
        foreach ($s in $strays) {
            Write-Host ("停止遗留 ffmpeg PID {0}" -f $s.ProcessId)
            Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Host ("（枚举进程失败，跳过遗留 ffmpeg 清理：{0}）" -f $_.Exception.Message)
    }
    Write-Host '已停止。'
}

if ($Stop) { Stop-All; return }
if ($RoomId -le 0) { throw '必须指定 -RoomId（例如 -RoomId 12345678）' }

if (-not $CookieFile) { $CookieFile = Join-Path $root 'bili_cookies.json' }
if (-not (Test-Path -LiteralPath $CookieFile)) {
    Write-Warning ("Cookie 文件不存在：{0} —— 弹幕会以未登录状态抓取" +
                   "（uid 全 0、昵称打码，@ 回复与个人记忆失效）。" +
                   "先跑 bili_login.py 扫码登录。" -f $CookieFile)
}

# 端口占用检查：Windows 允许重复绑定，两个服务同时监听会导致请求随机分发。
# 只看 LISTENING —— TIME_WAIT/CLOSE_WAIT 是刚才那次连接的残留，不影响重新绑定
# （Python 的 HTTPServer 默认开 allow_reuse_address）。
if ($WebOnly) {
    # 只重启决策/前端：采集与转录不动，免得为改一个开关把弹幕连接和
    # 转录流水都打断（模型也不用重新加载）
    if ($roles.ContainsKey('web')) {
        $old = Get-Process -Id $roles['web'] -ErrorAction SilentlyContinue
        if ($old) {
            Write-Host ("只重启决策进程：停止 PID {0}" -f $roles['web'])
            Stop-Process -Id $roles['web'] -Force
            Start-Sleep -Seconds 2
        }
    } else {
        Write-Host '（没找到记录的决策进程 PID，按新进程启动）'
    }
}

if ($AsrOnly) {
    if ($roles.ContainsKey('asr')) {
        $old = Get-Process -Id $roles['asr'] -ErrorAction SilentlyContinue
        if ($old) {
            Write-Host ("只重启转录进程：停止 PID {0}" -f $roles['asr'])
            Stop-Process -Id $roles['asr'] -Force
            Start-Sleep -Seconds 2
        }
    } else {
        Write-Host '（没找到记录的转录进程 PID，按新进程启动）'
    }
    # 强杀 Python 不会带走 ffmpeg 子进程；它还占着 asr_chunks 目录，
    # 继续跑只会白占 CPU/磁盘，新进程也会用新目录，没有保留价值。
    try {
        $strays = Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" -ErrorAction Stop |
                  Where-Object { $_.CommandLine -like '*asr_chunks*' }
        foreach ($s in $strays) {
            Write-Host ("停止遗留 ffmpeg PID {0}" -f $s.ProcessId)
            Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Host ("（枚举进程失败，跳过遗留 ffmpeg 清理：{0}）" -f $_.Exception.Message)
    }
}

# 只重启转录时不碰前端，也就不该被前端端口占用挡住
if (-not $AsrOnly) {
    $busy = netstat -ano | Select-String (":{0}\s+\S+\s+\S+\s+LISTENING" -f $Port) |
            Select-Object -First 1
    if ($busy) {
        throw ("端口 {0} 已被占用（{1}）。先停掉旧进程，或用 -Port 换一个端口。" -f
               $Port, $busy.ToString().Trim())
    }
}

$danmakuLog   = Join-Path $root ("danmaku_{0}_live.txt" -f $RoomId)
$transcriptLog = Join-Path $root ("transcript_{0}_live.txt" -f $RoomId)
$whisperModel = 'Systran/faster-whisper-large-v3'

# ---- ① 弹幕抓取：-Seconds 0 不限时，-Append 断线重连不覆盖 ----
if (-not $WebOnly -and -not $AsrOnly) {
$psArgs = @('-ExecutionPolicy', 'Bypass', '-NoProfile', '-File',
            (Join-Path $root 'integrations\bili_danmaku.ps1'),
            '-RoomId', "$RoomId", '-OutFile', $danmakuLog,
            '-Seconds', '0', '-Append', '-CookieFile', $CookieFile)
$p1 = Start-Process -WindowStyle Hidden -PassThru -FilePath 'powershell.exe' `
      -ArgumentList $psArgs -WorkingDirectory $root `
      -RedirectStandardOutput (Join-Path $runDir 'danmaku.log') `
      -RedirectStandardError  (Join-Path $runDir 'danmaku.err')
$roles['danmaku'] = $p1.Id
Write-Host ("① 弹幕抓取 PID {0} → {1}" -f $p1.Id, $danmakuLog)
} else {
    Write-Host ("① 弹幕抓取：保持运行（PID {0}）" -f $roles['danmaku'])
}

# ---- ② 语音转录：本地 large-v3 + 人声分离 + VAD + 唱歌过滤 ----
if ($WebOnly) {
    Write-Host ("② 语音转录：保持运行（PID {0}）" -f $roles['asr'])
} elseif (-not $NoAsr) {
    $asrArgs = @('-u', '-m', 'integrations.bili_live_asr',
                 '--room-id', "$RoomId",
                 '--model', $whisperModel,
                 '--separate', '--vad', '--singing-filter', '--chunk', '8',
                 '--wait-live', '--wait-interval', "$WaitLiveInterval",
                 '--out', $transcriptLog)
    $p2 = Start-Process -WindowStyle Hidden -PassThru -FilePath $PythonExe `
          -ArgumentList $asrArgs -WorkingDirectory $root `
          -RedirectStandardOutput (Join-Path $runDir 'asr.log') `
          -RedirectStandardError  (Join-Path $runDir 'asr.err')
    $roles['asr'] = $p2.Id
    Write-Host ("② 语音转录 PID {0}（未开播会等待）→ {1}" -f $p2.Id, $transcriptLog)
} else {
    Write-Host '② 语音转录：已跳过（-NoAsr）'
}

# ---- ③ 决策 + 前端 ----
if ($AsrOnly) {
    Write-Host ("③ 决策/前端：保持运行（PID {0}）" -f $roles['web'])
} else {
$webArgs = @('-u', '-m', 'agent.cli', 'web',
             '--room', "$RoomId", '--port', "$Port",
             '--danmaku-log', $danmakuLog,
             '--transcript-log', $transcriptLog,
             '--backfill', '0')
if ($MonitorOnly) {
    $webArgs += '--monitor-only'
    if ($Kb) { $webArgs += '--kb' }
}
elseif ($Send)         { $webArgs += '--send' }
$p3 = Start-Process -WindowStyle Hidden -PassThru -FilePath $PythonExe `
      -ArgumentList $webArgs -WorkingDirectory $root `
      -RedirectStandardOutput (Join-Path $runDir 'web.log') `
      -RedirectStandardError  (Join-Path $runDir 'web.err')
$roles['web'] = $p3.Id
$mode = if ($MonitorOnly) { '纯监控' } elseif ($Send) { '自动回复（会真的发弹幕）' } else { '影子模式（只生成不发送）' }
Write-Host ("③ 决策/前端 PID {0} | 模式: {1}" -f $p3.Id, $mode)
}

$pidLines = @()
foreach ($k in 'danmaku', 'asr', 'web') {
    if ($roles.ContainsKey($k) -and $roles[$k]) {
        $pidLines += ("{0}={1}" -f $k, $roles[$k])
    }
}
Set-Content -LiteralPath $pidFile -Value ($pidLines -join "`n") -Encoding utf8
Write-Host ''
Write-Host ("界面: http://127.0.0.1:{0}" -f $Port)
Write-Host ("日志: {0}" -f $runDir)
Write-Host ("停止: powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -Stop")
