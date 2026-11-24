"""直播智能互动 Agent —— 情报库 + 影子模式决策 + 回放。

模块划分:
  store     情报库（SQLite schema / 落库 / 查询）
  clean     文本清洗（繁转简、机器人过滤、幻觉过滤、噪声过滤）
  guard     发送护栏（长度/频率/重复/屏蔽词）
  llm       LLM 客户端（网关）与离线占位
  decide    决策管线（触发 → 上下文 → 生成 → 护栏 → 审计）
  ingest    把抓取产物导入情报库
  replay    回放器
"""

__all__ = ["store", "clean", "guard", "llm", "decide", "ingest", "replay"]
