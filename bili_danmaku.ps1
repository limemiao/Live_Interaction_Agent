param(
    [Parameter(Mandatory = $true)][int]$RoomId,
    [int]$Seconds = 45,
    [switch]$ShowPackets,
    [int]$ProtoVer = 2,
    [string]$OutFile,
    [switch]$Append,
    # 默认用脚本同目录的凭证。**不要留空**：未登录连接时 B 站不会投放
    # 用户信息（uid 全 0、昵称打码），下游就 @ 不了人、也存不了个人记忆。
    [string]$CookieFile = (Join-Path $PSScriptRoot 'bili_cookies.json'),
    # 测试用：连上这么多秒后主动断开一次，验证重连路径（0=关闭）
    [int]$ForceReconnectAfter = 0
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
$script:sw = $null

function TS { return (Get-Date).ToString('HH:mm:ss') }

# 实时落盘：每写一行立刻 flush，进程被强杀、断电也不会丢掉已收到的数据
function Log([string]$text) {
    Write-Output $text
    if ($null -ne $script:sw) {
        $script:sw.WriteLine($text)
        $script:sw.Flush()
    }
}

if ($OutFile) {
    $dir = Split-Path -Parent $OutFile
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $enc = New-Object System.Text.UTF8Encoding($true)
    $script:sw = New-Object System.IO.StreamWriter($OutFile, $Append.IsPresent, $enc)
    $script:sw.AutoFlush = $true
    if (-not $Append) {
        Log ("# 直播间 {0} 弹幕记录开始于 {1}" -f $RoomId, (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
    }
}

function Get-Int32BE([byte[]]$b, [int]$o) {
    return ([long]$b[$o] * 16777216) + ([long]$b[$o + 1] * 65536) + ([long]$b[$o + 2] * 256) + [long]$b[$o + 3]
}

function Get-BytesBE([int]$v) {
    $b = [BitConverter]::GetBytes($v); [Array]::Reverse($b); return $b
}

function Get-BytesBE16([int]$v) {
    $b = [BitConverter]::GetBytes([int16]$v); [Array]::Reverse($b); return $b
}

function New-Packet([int]$op, [byte[]]$body) {
    if ($null -eq $body) { $body = New-Object 'byte[]' 0 }
    $total = 16 + $body.Length
    $p = New-Object 'byte[]' $total
    [Array]::Copy((Get-BytesBE $total), 0, $p, 0, 4)
    [Array]::Copy((Get-BytesBE16 16), 0, $p, 4, 2)
    [Array]::Copy((Get-BytesBE16 1), 0, $p, 6, 2)
    [Array]::Copy((Get-BytesBE $op), 0, $p, 8, 4)
    [Array]::Copy((Get-BytesBE 1), 0, $p, 12, 4)
    if ($body.Length -gt 0) { [Array]::Copy($body, 0, $p, 16, $body.Length) }
    return $p
}

function Expand-Zlib([byte[]]$data) {
    $ms = New-Object System.IO.MemoryStream(, $data)
    $ms.Position = 2
    $ds = New-Object System.IO.Compression.DeflateStream($ms, [System.IO.Compression.CompressionMode]::Decompress)
    $out = New-Object System.IO.MemoryStream
    $ds.CopyTo($out)
    $ds.Dispose(); $ms.Dispose()
    return $out.ToArray()
}

$script:danmaku = 0
$script:gifts = 0
$script:entries = 0
$script:showRaw = $ShowPackets

function Handle-Json([string]$json) {
    try { $m = $json | ConvertFrom-Json } catch { return }
    if ($null -eq $m.cmd) { return }
    if ($script:showRaw) { Write-Output ("  cmd=" + $m.cmd + " | " + $json.Substring(0, [Math]::Min(4000, $json.Length))) }
    switch ($m.cmd) {
        'DANMU_MSG' {
            $script:danmaku++
            $uid = $m.info[2][0]
            $uname = $m.info[2][1]
            $medal = ''
            if ($m.info.Count -gt 3 -and $m.info[3].Count -gt 1) { $medal = $m.info[3][1] }
            $medalLv = ''
            if ($m.info.Count -gt 3 -and $m.info[3].Count -gt 0) { $medalLv = $m.info[3][0] }
            # 回复关系：B 站客户端的 @ 是渲染出来的，原始文本里没有 @，
            # 回复目标藏在 info[0][15].extra 的 JSON 里，必须单独取出来。
            $replyTag = ''
            try {
                if ($m.info[0].Count -gt 15 -and $m.info[0][15].extra) {
                    $ex = $m.info[0][15].extra | ConvertFrom-Json
                    if ($ex.reply_mid) {
                        $replyTag = "reply=$($ex.reply_mid):$($ex.reply_uname) | "
                    }
                }
            } catch { }
            Log ("[{0}] [弹幕] {1} | uid={2} | medal={3} lv={4} | {5}{6}" -f (TS), $uname, $uid, $medal, $medalLv, $replyTag, $m.info[1])
        }
        'SEND_GIFT' {
            $script:gifts++
            # 带上 uid：下游要能 @ 回送礼的人（和弹幕行格式保持一致）
            Log ("[{0}] [礼物] {1} | uid={2} | 送出 {3} x{4}" -f (TS), $m.data.uname, $m.data.uid, $m.data.giftName, $m.data.num)
        }
        'SUPER_CHAT_MESSAGE' {
            Log ("[{0}] [SC] {1} | uid={2} | {3}" -f (TS), $m.data.user_info.uname, $m.data.uid, $m.data.message)
        }
        # 现在的房间推的是 INTERACT_WORD_V（老的 INTERACT_WORD 已不再出现），
        # 只判老名字会导致"进入直播间"一直收不到（实测历史文件里 0 条）
        { $_ -in 'INTERACT_WORD', 'INTERACT_WORD_V' } {
            $script:entries++
            Log ("[{0}] [进入] {1} | uid={2} | 进入直播间" -f (TS), $m.data.uname, $m.data.uid)
        }
    }
}

# --- WBI 签名，用于换取弹幕服务器地址和 token --------------------------------------
$sess = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$baseHeaders = @{ 'User-Agent' = $UA; 'Referer' = "https://live.bilibili.com/$RoomId" }

$script:loginUid = 0
if ($CookieFile) {
    if (-not (Test-Path -LiteralPath $CookieFile)) {
        # 不退出：仍然能收弹幕，但要明确告诉使用者会丢失用户信息
        Log "[$((TS))] [登录] Cookie 文件不存在: $CookieFile"
        Log "[$((TS))] [警告] 将以【未登录】状态连接：B 站不会投放 uid/昵称" +
            "（uid 全是 0、昵称打码），机器人无法 @ 回复、也无法记录个人记忆。"
    }
    else {
        $ck = Get-Content -LiteralPath $CookieFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $n = 0
        foreach ($p in $ck.cookies.PSObject.Properties) {
            try {
                $sess.Cookies.Add((New-Object System.Net.Cookie($p.Name, [string]$p.Value, '/', '.bilibili.com')))
                $n++
            } catch {
                Log "[$((TS))] [登录] Cookie 跳过 $($p.Name): $($_.Exception.Message)"
            }
        }
        if ($ck.uid) { $script:loginUid = [long]$ck.uid }
        Log "[$((TS))] [登录] 已加载 $n 个 Cookie, uid=$($ck.uid) uname=$($ck.uname) is_login=$($ck.is_login)"
    }
} else {
    Log "[$((TS))] [警告] 没有指定 -CookieFile，将以【未登录】状态连接" +
        "（uid 全 0、昵称打码，无法 @ 回复、无法记录个人记忆）"
}

# 预热（拿 buvid 等 Cookie），只做一次
Invoke-WebRequest -Uri 'https://www.bilibili.com/' -UseBasicParsing -Headers $baseHeaders -WebSession $sess -TimeoutSec 30 | Out-Null

# --- 重连框架 ----------------------------------------------------------------------
# 登录只做一次；"取签名 → 连服务器 → 收包"整段放在循环里，断了就重来。
# 原因：一场几小时的直播，网络抖动或服务端踢连接都会让收包循环 break，
# 旧版本直接结束进程（而它跑在隐藏窗口里，没人会注意到）。
$deadline = if ($Seconds -gt 0) { (Get-Date).AddSeconds($Seconds) } else { [datetime]::MaxValue }
$attempt = 0
$backoff = 3              # 指数退避 3→6→12→24→48→60（秒），连上后重置
$stop = $false

while (-not $stop -and (Get-Date) -lt $deadline) {
$attempt++
$reason = ''

try {
$nav = (Invoke-WebRequest -Uri 'https://api.bilibili.com/x/web-interface/nav' -UseBasicParsing -Headers $baseHeaders -WebSession $sess -TimeoutSec 30).Content | ConvertFrom-Json
$imgKey = [System.IO.Path]::GetFileNameWithoutExtension($nav.data.wbi_img.img_url)
$subKey = [System.IO.Path]::GetFileNameWithoutExtension($nav.data.wbi_img.sub_url)
$tab = 46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
$raw = $imgKey + $subKey
$mixinKey = (-join ($tab | ForEach-Object { $raw[$_] })).Substring(0, 32)

$wts = [int][double]::Parse((Get-Date -UFormat %s))
$signParams = @{ id = $RoomId; type = 0; web_location = '444.1'; wts = $wts }
$query = ($signParams.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$([uri]::EscapeDataString([string]$_.Value))" }) -join '&'
$md5 = [System.Security.Cryptography.MD5]::Create()
$wrid = [BitConverter]::ToString($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($query + $mixinKey))).Replace('-', '').ToLower()

$info = (Invoke-WebRequest -Uri "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?$query&w_rid=$wrid" -UseBasicParsing -Headers $baseHeaders -WebSession $sess -TimeoutSec 30).Content | ConvertFrom-Json
if ($info.code -ne 0) {
    # token 有时效，重连必须重取；这里用 throw 交给下面的 catch 统一退避
    throw "getDanmuInfo code=$($info.code) msg=$($info.message)"
}
} catch {
    $reason = "取弹幕服务器失败: $($_.Exception.Message)"
    Log ("[{0}] [重连] {1}（第 {2} 次，等待 {3}s）" -f (TS), $reason, $attempt, $backoff)
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min(60, $backoff * 2)
    continue
}

$host0 = $info.data.host_list[0]
$token = $info.data.token
Log "[$((TS))] [连接] server=$($host0.host) wss_port=$($host0.wss_port) token_ok=$([bool]$token)"

$buvid = ($sess.Cookies.GetCookies('https://www.bilibili.com') | Where-Object { $_.Name -eq 'buvid3' } | Select-Object -First 1).Value
if (-not $buvid) {
    $buvid = [guid]::NewGuid().ToString().ToUpper() + 'infoc'
}
Log "[$((TS))] [连接] buvid=$buvid"

# --- WebSocket 连接 ----------------------------------------------------------------
$cts = New-Object System.Threading.CancellationTokenSource
$ws = $null
foreach ($h in $info.data.host_list) {
    $tryWs = New-Object System.Net.WebSockets.ClientWebSocket
    $tryWs.Options.SetRequestHeader('Origin', 'https://live.bilibili.com')
    $tryWs.Options.Cookies = $sess.Cookies
    $uri = [Uri]"wss://$($h.host):$($h.wss_port)/sub"
    Log "[$((TS))] [连接] $uri ..."
    try {
        $tryWs.ConnectAsync($uri, $cts.Token).Wait(12000) | Out-Null
    } catch {
        Log ("[$((TS))] [连接] 失败: " + $_.Exception.Message)
    }
    if ($tryWs.State -eq 'Open') { $ws = $tryWs; $host0 = $h; break }
    $tryWs.Dispose()
}
if ($null -eq $ws) {
    # 连不上就退避重试，而不是退出进程
    $reason = '所有弹幕服务器都连不上'
    Log ("[{0}] [重连] {1}（第 {2} 次，等待 {3}s）" -f (TS), $reason, $attempt, $backoff)
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min(60, $backoff * 2)
    continue
}
Log "[$((TS))] [连接] ws_state=$($ws.State) host=$($host0.host)"
if (-not $script:loginUid) {
    Log "[$((TS))] [警告] 本次连接没有登录态（loginUid=0）——收到的弹幕" +
        "uid 会全是 0、昵称打码，@ 回复与个人记忆都会失效。"
}

$authJson = @{ uid = $script:loginUid; roomid = $RoomId; protover = $ProtoVer; buvid = $buvid; platform = 'web'; type = 2; key = $token } | ConvertTo-Json -Compress
$ws.SendAsync((New-Object ArraySegment[byte] -ArgumentList (, (New-Packet 7 ([Text.Encoding]::UTF8.GetBytes($authJson))))), 'Binary', $true, $cts.Token).Wait(10000) | Out-Null

# 连上了：退避重置，记录这一轮连接的开始时间
$backoff = 3
$connStart = Get-Date

$heartbeat = New-Packet 2 ([Text.Encoding]::UTF8.GetBytes('{}'))
$buf = New-Object 'byte[]' 65536
$pending = New-Object System.Collections.Generic.List[byte]
$lastHeartbeat = Get-Date
$lastData = Get-Date          # 最近一次收到任何数据（含心跳回包）
$recvTask = $null
$lastMark = Get-Date
$script:popularity = 0
if ($Seconds -le 0 -and $attempt -eq 1) { Log "[$((TS))] [状态] 不限时模式，按 Ctrl+C 结束" }

while ((Get-Date) -lt $deadline) {
    # 半开连接：socket 还在但对方早已不回包，ReceiveAsync 会一直挂着。
    # 心跳每 25 秒一次、服务端会回 op=3，所以 90 秒收不到任何数据就是死了。
    if (((Get-Date) - $lastData).TotalSeconds -ge 90) {
        $reason = '90 秒没有收到任何数据（疑似半开连接）'
        break
    }
    # 测试开关：连上 N 秒后主动断开，用来验证重连路径（不影响正常使用）
    if ($ForceReconnectAfter -gt 0 -and ((Get-Date) - $connStart).TotalSeconds -ge $ForceReconnectAfter) {
        $reason = "测试：连上 $ForceReconnectAfter 秒后主动断开"
        Log ("[{0}] [测试] {1}" -f (TS), $reason)
        break
    }
    # 每 60 秒写一行状态：既是运行记录，也是给下游看门狗的"心跳"
    # （日志文件不再增长 = 抓取进程死了，下游据此报警）
    if (((Get-Date) - $lastMark).TotalSeconds -ge 60) {
        Log ("[{0}] [状态] 已收弹幕 {1} 条, 礼物 {2} 条, 人气 {3}" -f (TS), $script:danmaku, $script:gifts, $script:popularity)
        $lastMark = Get-Date
    }
    if (((Get-Date) - $lastHeartbeat).TotalSeconds -ge 25) {
        try { $ws.SendAsync((New-Object ArraySegment[byte] -ArgumentList (, $heartbeat)), 'Binary', $true, $cts.Token).Wait(5000) | Out-Null } catch { }
        $lastHeartbeat = Get-Date
    }

    if ($null -eq $recvTask) {
        $recvTask = $ws.ReceiveAsync((New-Object ArraySegment[byte] -ArgumentList (, $buf)), $cts.Token)
    }
    if (-not $recvTask.Wait(1000)) { continue }
    try { $res = $recvTask.Result } catch { $reason = "接收出错: $($_.Exception.Message)"; break }
    $recvTask = $null
    if ($res.MessageType -eq 'Close') { $reason = '服务端关闭连接'; break }
    $lastData = Get-Date
    for ($i = 0; $i -lt $res.Count; $i++) { $pending.Add($buf[$i]) }

    while ($pending.Count -ge 16) {
        $arr = $pending.ToArray()
        $total = Get-Int32BE $arr 0
        if ($total -le 0 -or $total -gt 5000000) { $pending.Clear(); break }
        if ($pending.Count -lt $total) { break }

        $hdr = ([int]$arr[4] * 256) + [int]$arr[5]
        $ver = ([int]$arr[6] * 256) + [int]$arr[7]
        $op = Get-Int32BE $arr 8
        $payload = New-Object 'byte[]' ([int]($total - $hdr))
        [Array]::Copy($arr, $hdr, $payload, 0, $payload.Length)
        $pending.RemoveRange(0, [int]$total)

        if ($ShowPackets) { Write-Output ("PKT op=$op ver=$ver body=$($payload.Length)") }

        if ($op -eq 5 -and $ver -eq 2) {
            $inner = Expand-Zlib $payload
            for ($i = 0; $i -lt $inner.Length;) {
                $it = Get-Int32BE $inner $i
                $ih = ([int]$inner[$i + 4] * 256) + [int]$inner[$i + 5]
                $io = Get-Int32BE $inner ($i + 8)
                if ($it -le 0 -or $i + $it -gt $inner.Length) { break }
                if ($io -eq 5) { Handle-Json ([Text.Encoding]::UTF8.GetString($inner, $i + $ih, $it - $ih)) }
                $i += $it
            }
        }
        elseif ($op -eq 5 -and ($ver -eq 0 -or $ver -eq 1)) {
            Handle-Json ([Text.Encoding]::UTF8.GetString($payload))
        }
        elseif ($op -eq 3) {
            $pop = if ($payload.Length -eq 4) { Get-Int32BE $payload 0 } else { [Text.Encoding]::UTF8.GetString($payload) }
            $script:popularity = $pop
            if ($ShowPackets) { Write-Output "[心跳] popularity=$pop" }
        }
        elseif ($op -eq 8) {
            Log "[$((TS))] [状态] 认证成功"
        }
    }
}

# --- 这一轮结束了：是到点收工，还是断了要重连 --------------------------------------
try { $ws.Dispose() } catch { }
$ws = $null
$recvTask = $null
$pending.Clear()

if ((Get-Date) -ge $deadline) { break }
if (-not $reason) { $reason = '连接中断（未知原因）' }
Log ("[{0}] [重连] {1}（第 {2} 次，等待 {3}s）" -f (TS), $reason, $attempt, $backoff)
Start-Sleep -Seconds $backoff
$backoff = [Math]::Min(60, $backoff * 2)
}   # ← 重连循环结束

Log ("[{0}] [汇总] 弹幕 {1} 条, 礼物 {2} 条, 进入 {3} 条" -f (TS), $script:danmaku, $script:gifts, $script:entries)
if ($script:sw) {
    $script:sw.Flush()
    $script:sw.Dispose()
    $script:sw = $null
    Write-Output "已实时写入 $OutFile"
}
if ($ws) { try { $ws.Dispose() } catch { } }
