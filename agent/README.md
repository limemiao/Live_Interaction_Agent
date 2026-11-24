# agent —— 直播情报库与影子决策（v1）

> ⚠️ **本文件描述的是 v1 阶段的形态，多处已过时**（例如"这一版不发送任何弹幕"、
> "只有 FTS5 全文检索"）。当前完整说明见项目根目录的 `README.md`，
> 本文件保留作为演进记录。

对应设计文档 `docs/agent_design.md` 的 **P1~P4**：
情报库、清洗层、影子模式决策、回放器。

**这一版不发送任何弹幕。** 所有输出都写进 decisions 表供人工查看，
发送功能按设计文档的建议留到 P5 之后再开。

## 快速开始

```powershell
cd live_interaction_agent

# 0. 配置：编辑根目录的 config.yaml（API、模型、思考程度、长度上限、机器人名单）

# 1. 导入抓取产物（弹幕日志 + 语音转录）
python -m agent.cli ingest --danmaku danmaku_login_test.txt --date 2026-10-13 --session demo
python -m agent.cli ingest --transcript transcript_xxx.txt --date 2026-10-13 --session demo

# 2. 看概况
python -m agent.cli stats
python -m agent.cli users --limit 10
python -m agent.cli search --query 模拟器

# 3. 回放 + 影子决策（离线占位模型，不联网、不花钱）
python -m agent.cli replay --session <id> --offline

# 4. 用真实模型（读 config.yaml，只生成不发送）
python -m agent.cli replay --session <id>

# 5. 查看候选
python -m agent.cli decisions --limit 20
```

## 配置文件（config.yaml）

| 段 | 关键项 | 说明 |
|---|---|---|
| `llm` | `base_url` / `api_key` / `model` | DeepSeek OpenAI 兼容入口；key 按使用要求明文放置 |
| `llm.thinking` | `enabled` / `param` / `value` | 思考程度，默认 `low`；不支持的实现会自动降级重试 |
| `reply` | `hard_limit_units` / `target_chars` | 长度硬上限（加权单位）与建议长度 |
| `reply` | `bot_name` / `bot_uid` | 机器人自己的昵称与 uid（uid 用来在上下文里区分"我上一句"和别人的话） |
| `reply.persona` | — | 「普通观众」人设的风格段（替代 `personas.py` 里的 normal 文案；身份与不可变底线仍是代码写死的）。切到其它人设或操作者 `switch_persona` 后不生效 |
| `triggers` | `keywords` / `types` | 触发条件 |
| `context` | `history_top_k` / `min_score` | 语义检索取几条历史、相似度下限（低于阈值视为"没有相关历史"） |
| `guard` | 频率与重复阈值 | 发送护栏 |
| `clean` | `bot_uids` / `bot_nicknames` / `bot_reply_patterns` | 机器人识别 |

> 2026-10-16 更新：语义检索已在实时链路（`web` / `watch` / `cli replay`）接通，
> 拿不到索引时自动退化为只用近场上下文；可用 `--no-index` 显式关闭。
> 该轮修复细节见项目历史记录（未随仓库分发）。
> 下面"尚未实现"表里的"向量检索"一项已过时。

长度约束会**写进给模型的提示词**（见 `config.reply_length_hint`），例如：

```
输出长度硬上限：40 个汉字以内（数字、字母按半个字算）——超过这个长度直播间会直接拒收。
建议控制在 15 个字以内，弹幕越短越像真人。
```

## 模块

| 模块 | 职责 |
|---|---|
| `store.py` | SQLite schema、落库、查询（events / users / sessions / decisions + FTS5） |
| `clean.py` | 繁转简、房管话术过滤、Whisper 幻觉过滤、噪声过滤 |
| `guard.py` | 长度（实测加权规则）、频率、重复、屏蔽词、身份泄露 |
| `llm.py` | 网关客户端 + 离线占位模型 |
| `decide.py` | 触发判定 → 上下文组装 → 生成 → 护栏 → 审计 |
| `ingest.py` | 解析 `bili_danmaku.ps1` / `bili_live_asr.py` 的日志并落库 |
| `replay.py` | 按时间轴回放，跑完整决策流程但不发送 |

## 设计要点

**清洗层是情报库质量的关键。** 实测例证：
导入 13 条弹幕时自动丢掉 2 条噪声 + 1 条房管自动回复话术；
导入 17 条语音时丢掉 1 条 Whisper 字幕幻觉。
这些如果不滤，后面的所有分析都会被污染。

**护栏是代码级硬约束**，不靠模型自觉。长度规则来自实测：
汉字算 1、字母算 0.5、上限 40 单位。自检结果
（40 汉字通过 / 41 拒绝 / 80 字母通过 / 119 字母拒绝）
与真机实测完全一致。

**影子模式模拟真实节奏**。`SendState` 会把通过护栏的候选标记为"已发送"，
所以频率限制在回放时也按真实节奏生效——否则跑历史数据会瞬间发出几百条，
失去评估意义。

## 尚未实现（按设计文档的优先级）

| 缺口 | 对应阶段 |
|---|---|
| 采集稳定性：断流重连、开播自动跟踪、进程自愈 | P0 |
| 实时落库：目前是离线导入日志，未与抓取进程打通 | P1 |
| 说话人归属、话题/意图识别 | P2 |
| 向量检索（当前只有 FTS5 全文） | P1/P2 |
| 实际发送（`reply_mid` 回复） | P5+ |
| 评测集与人工打分流程 | P4 |

## 与抓取端的关系

采集端仍是现有脚本，先把日志落成文件，再由 `ingest` 导入：

```
bili_danmaku.ps1  → danmaku_*.txt  ┐
bili_live_asr.py  → transcript_*.txt ┴→ agent.cli ingest → events.db
```

下一步若要做实时，把 `ingest` 的解析逻辑改成"边收边写 store"即可，
表结构不用动。
