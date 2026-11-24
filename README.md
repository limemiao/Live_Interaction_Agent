# Live Interaction Agent

面向 B 站直播间的互动 Agent：实时采集弹幕和主播语音，建立本地情报库，按触发条件
决定是否回复，并用大模型生成一条符合人设和长度限制的弹幕，经过护栏与限速队列后
发出。

默认处于**影子模式**：只生成候选，不实际发送。加 `--Send` 才会真的发弹幕。

## 功能

- 弹幕采集：WebSocket 接入，记录弹幕、礼物、SC、进场和回复关系。
- 语音转录：faster-whisper large-v3，支持 VAD、人声分离、唱歌过滤和断流重连。
- 本地情报库：SQLite 事件表、观众画像、人物卡、房间事实、黑话、场次摘要。
- 语义检索：bge-small-zh 向量索引，按相关度召回历史上下文。
- 回复决策：触发规则、人设、护栏、去重、长度约束、发送队列与限速。
- 本地 Web 控制台：实时监控、历史回放、测试、知识库和手动发送。
- 操作者权限：只有指定 uid 能拉黑、切换人设或删除历史。

## 环境要求

- Windows 10 / 11。
- Python 3.10 或更高版本。
- PowerShell 7（或 Windows PowerShell 5.1）。
- FFmpeg / FFprobe 在 `PATH` 中。
- NVIDIA GPU + CUDA 可选；CPU 也能跑，但转录会慢很多。

主要依赖：

```powershell
python -m pip install torch torchaudio faster-whisper transformers demucs `
    soundfile scipy numpy requests huggingface_hub segno
```

CUDA 版 PyTorch 请按官方说明安装；仓库提供
`setup/cuda/setup_torch_cu126.ps1` 作为 CUDA 12.6 的示例。

首次运行会下载以下模型：

| 用途 | 模型 |
|---|---|
| 语音识别 | `Systran/faster-whisper-large-v3` |
| 中文向量 | `BAAI/bge-small-zh-v1.5` |
| 人声分离 | Demucs `htdemucs`（使用 `--separate` 时加载） |
| 唱歌检测 | `MIT/ast-finetuned-audioset-10-10-0.4593` |

`setup/models/download_large_v3.ps1` 与 `setup/models/download_bge.ps1` 可提前下载
前两个模型；Demucs 和 AST 模型会在首次使用时自动获取。缓存说明见
[setup/models/MODEL_CACHE.md](setup/models/MODEL_CACHE.md)。

## 配置

```powershell
copy config.example.yaml config.yaml
```

然后编辑 `config.yaml`：

- `llm.api_key`：模型 API key（也可用兼容网关的 `base_url`）。
- `reply.bot_name` / `reply.bot_uid`：机器人账号。
- `triggers.names` / `triggers.keywords`：触发词。
- `operator_uid`：拥有高权限工具的操作者 uid。
- `db`：SQLite 路径，默认 `data/events.db`。

`config.yaml`、`bili_cookies*.json`、`audio/`、`data/`、`output/` 和 `run/` 都已
加入 `.gitignore`，不会进入仓库。

## 登录

```powershell
python bili_login.py
```

扫码后，登录态写入 `bili_cookies.json`。弹幕抓取必须带登录态，否则 B 站会把
uid 和昵称抹成 0 或打码，机器人无法 @ 人，也无法建立观众记忆。

## 快速开始

```powershell
# 1) 建向量索引（本地运行，不调用模型）
python -m agent.cli index

# 2) 一键启动：弹幕 + 转录 + 决策/前端
powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -RoomId <房间号> -MonitorOnly

# 影子模式（生成候选，不发送）
powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -RoomId <房间号>

# 自动回复（通过护栏后真的发送）
powershell -ExecutionPolicy Bypass -File .\start_live.ps1 -RoomId <房间号> -Send
```

`start_live.ps1` 默认端口是 8765；如果该端口已被其他程序占用，加 `-Port 8766`。
`-Stop` 停止本脚本启动的进程，`-AsrOnly` / `-WebOnly` 可单独重启某一部分。

浏览器打开 <http://127.0.0.1:8765>（或你指定的端口）。

## 手动启动

```powershell
# 抓弹幕（-Seconds 0 表示不限时）
powershell -ExecutionPolicy Bypass -File .\bili_danmaku.ps1 `
    -RoomId <房间号> -OutFile danmaku_run.txt -Seconds 0 -Append

# 转录主播语音
python bili_live_asr.py --room-id <房间号> `
    --model Systran/faster-whisper-large-v3 `
    --separate --vad --singing-filter --chunk 8 `
    --out transcript_run.txt

# 决策服务 + Web 前端
python -m agent.cli web --room <房间号>
```

## 目录结构

```text
live_interaction_agent/
├─ agent/
│  ├─ watch.py        # 采集与实时链路
│  ├─ ingest.py       # 事件入库与清洗
│  ├─ store.py        # SQLite 存储、画像、记忆
│  ├─ embed.py        # 向量索引与检索
│  ├─ decide.py       # 触发、上下文、决策
│  ├─ guard.py        # 内容护栏
│  ├─ queue.py        # 发送队列与限速
│  ├─ execute.py      # 弹幕发送
│  ├─ personas.py     # 人设与不可变底线
│  ├─ llm.py          # 模型客户端
│  └─ web.py          # Web 控制台
├─ bili_danmaku.ps1   # 弹幕 WebSocket 采集
├─ bili_live_asr.py   # 语音转录
├─ bili_login.py      # 扫码登录
├─ bili_send_danmaku.py
├─ start_live.ps1     # 一键启动/停止
├─ config.example.yaml
├─ docs/              # 设计、研究与完整说明
└─ setup/             # CUDA / 模型下载脚本
```

## Web 控制台

| 页面 | 内容 |
|---|---|
| `/` | 实时弹幕、语音、决策候选、拒绝原因、发送结果、手动发送 |
| `/history` | 按日期/房间筛选场次，查看事件与搜索 |
| `/test` | 用已录制音频离线重跑转录 |
| `/kb` | 记忆、事实卡、人物卡、工具调用审计、决策记录 |

手动发送可选择机器人或操作者身份，同样经过护栏和队列。

## 数据与隐私

- 仓库不包含任何 Cookie、API key、弹幕记录、转录文本、录音或运行日志。
- `config.example.yaml` 中的账号、房间号和 uid 均为占位符，请替换为自己的。
- 运行时产生的数据全部落在已忽略的目录中，请自行评估保存和分享风险。

## 注意

- 发送频率必须遵守 B 站风控：默认至少 3 秒一条、每分钟最多 8 条。
- 建议先用 `-MonitorOnly` 或影子模式观察决策质量。
- 本项目仅用于学习和技术研究，请遵守平台规则和当地法律。

## 更多文档

- [docs/README-full.md](docs/README-full.md)：整理前的完整说明（架构、数据模型、
  配置项、实测记录）。
- [docs/bilibili_live_notes.md](docs/bilibili_live_notes.md)：B 站协议与踩坑记录。
- [docs/agent_design.md](docs/agent_design.md)：系统设计。
- [docs/agent_persona_and_tools.md](docs/agent_persona_and_tools.md)：人设与工具权限。
- [docs/agent_reply_optimization.md](docs/agent_reply_optimization.md)：回复质量优化。
- [docs/agent_extract_module.md](docs/agent_extract_module.md)：情报抽取模块。

## 许可

本仓库未附带开源许可证。模型、语音服务和 B 站接口的使用请遵守各自条款。
