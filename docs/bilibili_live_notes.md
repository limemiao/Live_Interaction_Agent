# B 站直播工具链：编码、发现与研究记录

> 时间跨度：2026-10-06 ~ 2026-10-14
> 工作目录：`live_interaction_agent`
> 目标：围绕一个 B 站直播间，打通「抓头像 → 生成图 → 抓弹幕 → 发弹幕 → 抓音频 → 实时语音转文字」整条链路。

---

## 0. 涉及的直播间与账号

| 名称 | 房间号 | uid | 备注 |
|---|---|---|---|
| 示例主播 | 123456789 | 1000000004 | 主要观察对象，会开播 |
| 操作者 | 12345678 | 1000000002 | 测试沙箱房间（未开播也能发弹幕、也能连弹幕服务器） |
| 示例机器人 | — | 1000000001 | 后期使用的登录账号，0 级新号 |

---

## 1. 文件清单

### 可直接使用的工具

| 文件 | 作用 |
|---|---|
| `bili_login.py` | 扫码登录，把 Cookie（含 SESSDATA）保存到 `bili_cookies.json`；换账号时自动备份旧凭证 |
| `bili_danmaku.ps1` | 实时抓弹幕，逐行落盘；支持登录态、不限时运行、断点追加 |
| `bili_send_danmaku.py` | 发送弹幕，支持 `--reply-mid` 回复某人 |
| `bili_live_asr.py` | 直播流实时语音转文字（ffmpeg → 可选 demucs 分离 → VAD → faster-whisper） |
| `setup/cuda/setup_torch_cu126.ps1` | 把 CPU 版 torch 换成 CUDA 12.6 版 |
| `setup/models/download_large_v3.ps1` | 下载 faster-whisper-large-v3 到 Hugging Face 缓存 |

### 实验脚本（`output/tmp/`）

`asr_selftest.py`（离线识别自检）、`asr_compare.py`（整段 vs 分片 × VAD 开关）、
`asr_tune.py`、`asr_antiloop.py`（防复读调参）、`asr_ablation.py`（原声 vs 分离）、
`pipeline_test.py`（四种管线组合）、`spk_experiment.py`（音色聚类）、
`spk_matrix.py`（音色相似度矩阵）、`loop_check.py`（音频重复性检测）、
`len_test.py`（长度上限）、`reply_test*.py`（回复机制）

### 产物

- 头像：`avatar_123456789_示例主播.jpg`（旧）、`avatar_123456789_示例主播_20261013.webp`（新）
- 生成图：`output/imagegen/elephant-*.png`（3 张）
- 弹幕记录：`danmaku_*.txt`
- 转录文本：`transcript_*.txt`
- 音频：`archive/media/stream_audio_123456789.mp3`、`output/tmp/bench_hifi.wav`、`output/tmp/demucs_out/.../vocals.wav`

---

## 2. 模块一：直播数据抓取

### 2.1 头像获取（最简单的一环）

```
GET https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room?roomid={roomid}
→ data.info.face 就是主播当前头像地址
```

**发现**：主播会换头像。10-06 抓到的是一张白毛猫帽特写（296×296 jpg），10-13 再抓已经变成另一张插画（webp）。所以「主播头像」不是一个稳定值，要按时间戳存。

### 2.2 扫码登录（`bili_login.py`）

用 B 站 passport 的二维码接口，全程不需要在聊天里粘贴凭证：

```
1. GET https://passport.bilibili.com/x/passport-login/web/qrcode/generate
   → data.url（二维码内容）、data.qrcode_key
2. 用 segno 把 url 渲染成本地 PNG，手机 B 站 App 扫码
3. GET .../qrcode/poll?qrcode_key=xxx 轮询
4. 成功后 Cookie（SESSDATA / bili_jct / DedeUserID / buvid3 …）写入本地 JSON
```

**踩坑（重要）**：poll 接口有**两层 code**。外层 `code` 是接口状态（0 = 请求成功），
登录状态在内层 `data.code`：

| data.code | 含义 |
|---|---|
| 86101 | 未扫码 |
| 86090 | 已扫码待确认 |
| 0 | 登录成功 |
| 86038 | 二维码过期 |

一开始读了外层 code，导致二维码刚生成就判定"登录成功"。

**换账号**：重新跑一次即可。脚本会把旧凭证改名备份成 `bili_cookies_<昵称>.json`，
之后用 `-CookieFile` / `--cookie-file` 指定就能在两个账号间切换，不用重复扫码。

### 2.3 弹幕实时抓取（`bili_danmaku.ps1`）

#### 协议细节

弹幕走 WebSocket，但不是随便连的，要先用 WBI 签名换服务器地址和 token：

```
1. GET https://api.bilibili.com/x/web-interface/nav → wbi_img.img_url / sub_url
2. mixinKey = 按固定 64 位置换表重排 img_key+sub_key 后取前 32 位
3. 参数按 key 排序拼接 + mixinKey → MD5 → w_rid
4. GET https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo
   ?id={roomid}&type=0&web_location=444.1&wts={ts}&w_rid={w_rid}
   → data.host_list / data.token
```

不带 WBI 签名时接口直接返回 `code=-352`（风控）。

连上 `wss://{host}:{wss_port}/sub` 后，认证包 JSON：

```json
{"uid": <真实uid或0>, "roomid": ..., "protover": 2, "buvid": "...",
 "platform": "web", "type": 2, "key": "<token>"}
```

#### 封包格式（自行实现的二进制解析）

```
偏移  长度  含义
0     4     包总长（大端）
4     2     包头长度（固定 16）  ← 注意是 2 字节，不是 4
6     2     协议版本（0/1 = 原始 JSON，2 = zlib 压缩）
8     4     操作码（2=心跳，3=心跳回复，5=消息，7=认证，8=认证回复）
12    4     序号
16    ...   负载
```

op=5 且 ver=2 时，负载是 zlib，需要跳过头 2 字节再用 `DeflateStream` 解压；
里面还会再套一层同格式的小包，逐个解析。

#### 实现过程中踩的四个坑

1. **包头长度读错**：一开始按 4 字节读 `header_len`，把 `ver` 也吃进去了，解析全乱。
2. **漏了 ver=0**：B 站实际推的是未压缩的 `ver=0` 包，代码只处理了 1 和 2，导致"连上了却收不到任何弹幕"。
3. **重复发起接收**：`ReceiveAsync` 超时后没清理，又发起新的接收，抛 `InvalidOperationException`。改成超时就复用同一个 task。
4. **PowerShell 脚本的 BOM**：Windows PowerShell 5.1 按 GBK 读 `.ps1`，脚本里带中文又没 UTF-8 BOM 时，中文字符串会把引号撑坏 → 直接语法报错。**这个坑后面又踩了两次**，凡是用 apply_patch 新建的带中文的 `.ps1`，都要补 BOM。

#### 匿名 vs 登录态（关键发现）

| | 匿名连接 | 登录态连接 |
|---|---|---|
| 昵称 | 一半被打码成 `宿***` | 完整 |
| uid 字段 | **恒为 0** | 真实 uid |
| 弹幕历史接口 | 返回空数组 | 返回最近 10 条，含完整昵称 |

被抹掉的不只是昵称，**uid 也一起被置 0**，所以没法用 uid 反查——服务端是整条抹掉的。
服务端会推一条通知说明原因：`为保护用户隐私，未登录无法查看他人昵称`。
登录之后需要在 auth 包里填**真实 uid**（而不是 0）才生效。

#### 落盘策略

早期版本把所有内容攒在内存列表里、脚本结束才一次性写盘——中途断网/断电整轮白跑。
改成 `StreamWriter` + 逐行 flush，每来一条立刻落盘。
另外加了 `-Seconds 0` 不限时模式、`-Append` 断点续写、每 5 分钟一条 `[状态]` 心跳行。

#### 实测流量

- 冷清时段：150 秒 6 条
- 热闹时段：5 分钟 29 条（约 6 条/分钟）

房间接口里的 `online: 9085` 是"人气值"不是真实在线人数，别被误导。

### 2.4 发送弹幕（`bili_send_danmaku.py`）

```
POST https://api.live.bilibili.com/msg/send   (form-urlencoded)
bubble=0 & msg=... & color=16777215 & mode=1 & fontsize=25
& rnd=<时间戳> & roomid=<房间号>
& csrf=<bili_jct> & csrf_token=<bili_jct>
```

**光有 SESSDATA 不够**，必须有 `bili_jct` 作为 csrf token，否则 `-111`。

**未开播的房间也能发**——实测 `live_status=0` 时返回 `code=0`，并且能在历史接口里查到。
这让自己的房间成了一个随时可用的测试沙箱。

#### 长度上限（实测）

| 内容 | 长度 | 结果 |
|---|---|---|
| 汉字 | 10 / 20 / 21 / 25 / 30 / 31 / **40** | ✅ |
| 汉字 | **41** / 42 / 43 / 50 / 60 / 80 | ❌ `code=1003212 超出限制长度` |
| 英文 | 20 / 30 / 40 / 60 / **80** | ✅ |
| 英文 | **119** / 120 / 121 / 150 | ❌ 同上 |

**不是字节限制**：40 汉字 = 120 字节能过，119 英文 = 119 字节反而被拒。
最符合数据的解释是**加权计数：汉字算 1、英文字母算 0.5，上限 40 单位**——
40 汉字 = 40 单位、80 英文 = 40 单位，两组都正好卡在边界。

> 英文 81~118 的区间未收窄（那次授权被拒），阈值精度到这里。
> 上限与账号权限相关，0 级新号拿的是普通档，舰长/老爷更高。

#### 回复弹幕 / @用户（实测）

**文本里写 `@昵称` 完全无效**：

```
发送 "@操作者 测试回复A"           → reply_mid=0, reply_uname='', reply_type_enum=0
发送 "@主播 测试回复B"                → reply_mid=0, reply_uname='', reply_type_enum=0
```

必须显式带参数 `reply_mid=<目标uid>`：

```
发送 "@操作者 测试回复C" + reply_mid=1000000002
→ reply_mid=1000000002, reply_uname='操作者', reply_type_enum=1
```

即：**uid 由调用方给，昵称由服务端解析**，文本里的 `@昵称` 纯粹是显示用。
`reply_type_enum` 从 0 变 1 是判定回复是否成立的可靠标志。

**@主播没有独立的服务端概念**：把 `reply_mid` 填房主 uid 就是 @主播，
实测文本写 `@主播`、`reply_uname` 返回的仍是 `操作者`。
「主播」只是站内客户端的显示习惯，服务端只认 uid。

#### 频率风险

早期为了测长度上限，**90 秒内连发约 20 条**——这个频率已经属于容易触发风控的量级，
所幸全部返回 `code=0` 或长度错误，没有出现 `10030`。后续测试改为 **2 条间隔 60 秒**。
建议：单账号最小间隔 ≥30 秒、每分钟 ≤1 条，遇到 `10030` 走指数退避而不是重试。

---

## 3. 模块二：直播流与音频

### 3.1 取流地址

```
GET https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo
  ?room_id={roomid}&protocol=0,1&format=0,1,2&codec=0,1
  &qn=10000&platform=web&ptype=8&dolby=5&panorama=1
```

返回 FLV / HLS(ts) / HLS(fmp4) 三类地址；`qn=10000` 是原画档。
实际 URL = `url_info[0].host + codec.base_url + url_info[0].extra`。

**必须带 `Referer: https://live.bilibili.com/`**，否则 `bilivideo.com` 的 CDN 直接 403。
流地址是带签名的临时链接，会过期，长时间录制要定期重新获取。

### 3.2 只抽音频

```bash
ffmpeg -y -user_agent "<UA>" -referer "https://live.bilibili.com/" \
  -i "<FLV地址>" -t 20 -vn -c:a libmp3lame -b:a 128k out.mp3
```

`-vn` 丢掉视频轨，20 秒只有 322 KB，录一整小时约 55 MB。

---

## 4. 模块三：实时语音转文字

### 4.1 技术选型

本地部署，**音频不出本机**，不使用任何上游网关。最终组合：

```
ffmpeg 拉流 → 切片 → demucs 人声分离 → VAD → faster-whisper large-v3 → 带时间戳落盘
```

| 组件 | 版本 | 说明 |
|---|---|---|
| faster-whisper | 1.2.1 | CTranslate2 后端，独立于 torch |
| ctranslate2 | 4.8.1 | 自带 CUDA 后端 |
| demucs | 4.1.0 | htdemucs，GPU |
| 模型 | large-v3 | 2.9 GB，ModelScope 下载 |

### 4.2 VAD 陷阱（最重要的发现）

第一版用 `vad_filter=True`（faster-whisper 默认），结果**大量内容被整段丢弃**。
同一段 60 秒音频的对照实验：

| 配置 | 有输出分片 | 字符数 |
|---|---|---|
| 分片 + VAD | 7/12 | 56 |
| 分片 + 关 VAD | **12/12** | **108** |
| 整段 + VAD | 覆盖 34.6s/60s | 38 |
| 整段 + 关 VAD | 覆盖 33.4s/60s | 59 |

关键证据：关掉 VAD 后多出了 49~60 秒那一段**唱歌**内容。
Silero VAD 是按"安静说话"调参的，对唱歌、轻声、连麦背景音一律误杀。

但**关掉 VAD 会引入新问题**：音乐段 Whisper 开始产生重复幻觉。

### 4.3 Whisper 的字幕套话幻觉

large-v3 在纯音乐/静音段会稳定吐出训练数据里的字幕模板：

```
请不吝点赞 订阅 转发 打赏支持明镜与点点栏目
优优独播剧场——YoYo Television Series Exclusive
字幕 by 索兰娅
```

一度让我误判为"音频在循环"，用相关性检测排除了（相邻 5 秒切片相关性平均 **0.002**，
接近 0 说明内容一直在变）。本质是模型幻觉，不是内容重复。

**处理**：加幻觉黑名单，用**组合特征**匹配（如 `点赞.*订阅.*转发`）而不是单词匹配，
避免把主播真说"点个订阅"也误杀。命中就丢弃并打印 `(丢弃幻觉)` 保留可观测性。

### 4.4 人声分离的真正价值

直觉上人声分离是为了"让识别更准"，实测却不然：

| | 有内容分片 | 字符数 | 平均 logprob |
|---|---|---|---|
| A 原声 | 9/12 | 94 | -0.484 |
| B 分离人声 | 10/12 | 110 | -0.526 |

主播说话的段落两边**一字不差**，多出来的内容是歌词。
单看字符数会得出"分离没用"的结论——**这个结论是错的**。

**分离真正的价值在于让 VAD 恢复工作**：

```
原声 + VAD      [ 5-10s] 明镜及点点栏目      ← 音乐骗过 VAD，Whisper 吐字幕套话
分离人声 + VAD   [ 5-10s] 好                ← 音乐已被抠掉，VAD 正常工作
```

混音里音乐让 VAD 失效 → 只能关 VAD → 关了之后 Whisper 在音乐段幻觉。
**分离把音乐拿掉，VAD 就可靠了，整条链路才闭合**。

分离性能：htdemucs 在 4070 上处理 **64.35 秒音频约 1.3 秒，约 50 倍实时**。

### 4.5 音色（说话人）实验

用 speechbrain 的 ECAPA-TDNN 给每个切片提取说话人向量，余弦相似度矩阵：

```
同一首歌的两片 (40-45s ↔ 45-50s)        0.82
主播连续说话的三片 (25-30/30-35/35-40s)  0.65 ~ 0.75
不相关内容的相似度                        0.10 ~ 0.30
歌曲段 ↔ 主播说话段                       0.40 ~ 0.59
```

**结论**：判别带清晰（同源 0.6~0.8、异源 0.1~0.3），机制可用。
但歌曲段与说话段 0.40~0.59 偏高，**倾向于说明那首歌就是主播自己唱的**——
如果是这样，"用音色过滤掉歌曲"从根上不成立，因为那确实是她的声音。
（后续用户确认 `vocals.wav` 里基本全是主播人声，支持这个判断。）

**音色真正该用的地方**：区分主播 vs 连麦嘉宾/观众。
矩阵里 50-55 秒那片与说话段只有 0.34~0.45，明显低于说话段内部，
是一个"可能是另一个说话人"的线索。

### 4.6 延迟与吞吐实测

| 配置 | 每片处理耗时 | 说明 |
|---|---|---|
| small，5s 切片 | 0.3 ~ 1.1 s | 约 5~15 倍实时 |
| large-v3，5s 切片 | 0.5 ~ 1.0 s | 延迟 0.1~1.2 s |
| large-v3 + demucs，8s 切片 | 0.5 ~ 1.6 s | 含分离，端到端约 9~10 s |

端到端延迟 = 切片时长（等凑满）+ 处理时间。切片越短延迟越低但准确率越差，
8 秒是"句子完整度"和"延迟"的折中。**瓶颈从来不是算力，是模型能力**。

---

## 5. 模块四：图像生成

用 gptsapi 的 gpt-image-2 接口（用户提供 key）：

```
POST https://api.gptsapi.net/api/v3/openai/gpt-image-2/image-edit
  { "prompt": ..., "input_urls": ["<图片URL>"], "aspect_ratio": "1:1", "resolution": "2K" }
→ { data.id, urls.get }  ← 异步，轮询 GET urls.get 直到 status=completed

POST .../gpt-image-2/text-to-image   ← 文生图，无 input_urls
```

### 5.1 `input_urls` 的坑

反复实测后确认：**这个网关只实现了官方三种输入方式里的"完整 URL"**。

| 输入方式 | 结果 |
|---|---|
| 完整 URL（可被抓取） | ✅ |
| base64 Data URL（png/jpeg/webp、各种体积） | ❌ `File type not supported` |
| 裸 base64 字符串 | ❌ 同上 |
| Files API `/v1/files` | ❌ 404 |
| multipart 直传 `/v1/images/edits` | ❌ 404 |

报错规律反推出的网关逻辑：
**非 `http(s)` 开头的输入 → `File type not supported`；是 http(s) 但抓不到 → `Image fetch failed`**。

所以本地图片必须变成公开可访问的 URL 才能用——这也是为什么第一次改头像（B 站头像本身
就有 CDN 链接）一次成功，而后来改本地大象图时卡了很久。

> 附带排除：某"用 base64 传本地图"的教程文档，用其自带脚本 + 其自带参考图实测，
> 在当前网关上同样失败，说明该文档描述的行为已不成立。

### 5.2 提示词长度与内容策略

- gpt-image-2 对超长描述型 prompt 容易触发内容策略拦截（`flagged by website`），
  同一内容改写精简后即可通过。
- 文生图时把参考图的造型拆成结构化要点（材质、配色、配饰、风格、光线），
  比"照着这张图改"更可靠——因为文生图并不真的"看到"原图。

---

## 6. 环境配置与踩坑记录

### 6.1 CUDA 环境

本机：RTX 4070（12GB，compute capability 8.9）+ 驱动 591.86 + 已装 CUDA Toolkit 12.6。
原 torch 是 CPU 版，换成 `torch 2.11.0+cu126` / `torchaudio 2.11.0+cu126`
（**同版本号，只换构建**，避免版本漂移）。验证：`torch.cuda.is_available() → True`。

ctranslate2 有独立 CUDA 后端，不受 torch 影响，所以 faster-whisper 全程正常。

### 6.2 两个 Python 环境（容易踩）

**沙箱内** `python` → `python.exe`
**提权后** `python` → `python`（3.13.5）

两个环境的包完全不同。凡是需要联网的操作都会提权，因此实际生效的一直是 anaconda。
**建议：脚本里一律写绝对路径 `python`，不要写裸 `python`。**

### 6.3 汇总的坑

| 问题 | 现象 | 解法 |
|---|---|---|
| PowerShell 脚本 BOM | 中文导致语法报错 | 写入时用 `UTF8Encoding($true)` 补 BOM |
| Anaconda OpenMP 冲突 | `OMP: Error #15: libiomp5md.dll already initialized` | `KMP_DUPLICATE_LIB_OK=TRUE` |
| torchaudio 2.11 读文件 | `TorchCodec is required` | 改用 `soundfile` 读 |
| speechbrain 建软链 | `WinError 1314 客户端没有所需的特权` | `snapshot_download(local_dir=...)` + `LocalStrategy.COPY` |
| HF 镜像跳转超时 | hf-mirror.com 返回 302 后 0 字节 | **直连 huggingface.co 反而通**（约 5 MB/s） |
| PyTorch 镜像速度 | 阿里云 126 KB/s（2.6GB 要 5 小时） | 换 SJTU，4.8 MB/s（9 分钟） |
| ModelScope | — | 5.8 MB/s，大模型首选 |

### 6.4 硬件实测速度

| 操作 | 速度 |
|---|---|
| demucs htdemucs（4070） | 约 50 倍实时 |
| faster-whisper large-v3（4070） | 8 秒切片约 0.5~1.0 秒 |
| ECAPA 音色提取 | 秒级 |

---

## 7. 已知限制与待办

### 限制

1. **英文长度上限未收窄**（81~118 区间没测），汉字 40 已确认。
2. **@主播 与 @用户 在服务端完全等价**，没有独立概念。
3. Whisper 对**唱歌**的转写不可靠；若歌声是主播本人，音色过滤也不适用。
4. 流地址会过期，长时间录制需要定期重新取流；当前脚本检测到 ffmpeg 退出就收尾，
   **尚未实现自动重连取流**。
5. 弹幕与语音两条线目前是独立的，**还没有做时间轴对齐**（把"谁在什么时候说了什么"
   和"谁在什么时候发了弹幕"关联起来）。

### 建议的下一步

1. **发送节流器**：单账号最小间隔 30 秒、每分钟上限 1 条，`10030` 走指数退避。
2. **断流自动重连**：检测到 ffmpeg 退出后重新取流地址重启，并续写同一个文件。
3. **开播自动跟踪**：轮询 `live_status`，开播即启动转录，下播自动收尾。
4. **长样本音色标定**：用 10 分钟以上样本替代当前 11 片的小样本，
   把"是不是同一个人"的阈值标定出来。
5. **音色过滤落地**：在 ASR 之后加一道说话人相似度打分，低分丢弃，
   解决"连麦嘉宾/别人说话混进来"的问题。

---

## 8. 一句话总结

这条链路上真正的难点不在抓数据，而在**音频质量**：
直播间的常驻背景音乐同时破坏了两件事——把人声淹没（伤 ASR），
又骗过 VAD（伤切分）。**人声分离 + VAD + 大模型**这个组合是目前验证有效的解法，
而"用音色标签提升识别率"这个直觉需要修正为
——**音色不是喂给 ASR 的，是用来做前后置过滤的**。
