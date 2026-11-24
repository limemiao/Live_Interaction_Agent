# Live Interaction Agent

B 站直播间互动工具。采集弹幕和语音转录，写入本地 SQLite 事件库。互动模式根据触发条件检索上下文、生成回复，通过内容检查和队列后再处理。默认影子模式会生成候选，不会自动发弹幕。

## 安装

需要 Windows、PowerShell、Python 3.10+。使用语音转录时需要 FFmpeg 和 FFprobe。安装依赖：

```powershell
python -m pip install requests segno pyyaml numpy huggingface_hub
python -m pip install faster-whisper torch torchaudio transformers demucs soundfile scipy
```

可按 [模型缓存说明](setup/models/MODEL_CACHE.md) 提前准备 Whisper 和向量模型。默认转录入口使用已缓存的 Whisper 权重。人声分离和唱歌检测依赖 Demucs 与 AST。GPU 环境按所用 PyTorch 和 CUDA 版本安装。

## 配置并监控直播间

```powershell
Copy-Item config.example.yaml config.yaml
python -m integrations.bili_login
$roomId = Read-Host "输入直播间房间号"
.\start_live.ps1 -RoomId $roomId -MonitorOnly
```

扫码登录后，Cookie 会写到仓库根目录的 `bili_cookies.json`。监控界面默认位于 <http://127.0.0.1:8765>。端口冲突时加 `-Port 8766`。先检查弹幕和转录，再配置模型与回复触发方式。

| 配置 | 用途 |
| --- | --- |
| `llm` | API 地址、模型和密钥 |
| `reply`、`triggers` | 机器人身份、触发词与回复风格 |
| `guard`、`send` | 回复去重、发送频率和队列 |
| `operator_uid` | 高权限操作账号 |

## 运行模式

| 启动参数 | 行为 |
| --- | --- |
| `-MonitorOnly` | 接收与显示事件，默认不调用回复模型 |
| 仅 `-RoomId` | 影子模式，在后台生成候选 |
| `-Send` | 将符合条件的候选放入实际发送队列 |
| `-NoAsr` | 跳过语音转录 |
| `-AsrOnly`、`-WebOnly` | 单独重启转录或控制台 |
| `-Stop` | 停止启动脚本管理的进程 |

手动使用单项工具时可运行 `python -m integrations.bili_live_asr --help`、`python -m integrations.bili_record --help` 和 `python -m agent.cli --help`。PowerShell 弹幕入口在 `integrations/bili_danmaku.ps1`。历史索引可通过 `python -m agent.cli index` 建立。

## 排查启动问题

- 弹幕没有用户名或用户 ID 时，先检查登录态是否有效，再启动采集进程。
- 转录窗口没有更新时，检查直播间是否正在开播、FFmpeg 能否运行，以及模型缓存是否完整。相关日志在 `run/asr.log` 和 `run/asr.err`。
- 只想检查弹幕与页面时，可在监控模式加 `-NoAsr`。如使用虚拟环境，通过 `-PythonExe` 指定 Python 路径。

## 目录与数据流

`integrations/` 管理登录、弹幕、录制、转录和发送。 `agent/` 管理事件入库、检索、回复、队列和 Web 控制台。 `setup/` 提供模型和 GPU 环境辅助脚本。 `experiments/` 保存实验工具，设计细节见 [完整手册](docs/README-full.md)。

弹幕、语音、日志、Cookie 与 SQLite 数据可能包含账号信息和聊天内容，应保存在本机。使用远程模型时，配置选取的上下文会发给相应服务。项目未附带开源许可证。
