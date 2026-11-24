"""配置加载：读取 config.yaml，与默认值合并。

配置文件里 api_key 是明文（按使用要求），等同凭证，不要外传。
"""

import copy
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

DEFAULTS = {
    "llm": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-flash",
        "temperature": 0.8,
        "max_tokens": 200,
        "timeout": 60,
        "thinking": {"enabled": True, "param": "thinking_depth", "value": "low"},
    },
    "reply": {
        "hard_limit_units": 40,
        "target_chars": 15,
        "persona": "你是一个 B 站直播间的老观众，说话简短口语化，绝不编造事实。",
        "bot_name": "",
    },
    "triggers": {
        "keywords": [],
        "question_regex": True,
        "types": ["danmaku", "gift", "superchat"],
        "min_interval_s": 30,
    },
    "guard": {
        "min_interval_s": 30,
        "per_minute": 1,
        "per_hour": 20,
        "duplicate_threshold": 0.8,
    },
    "clean": {"bot_uids": [], "bot_nicknames": [], "bot_reply_patterns": []},
    "db": "data/events.db",
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    """加载配置；文件不存在时退回默认值。"""
    root = Path(__file__).resolve().parent.parent
    p = Path(path) if path else (root / "config.yaml")
    data = {}
    if p.exists():
        if yaml is None:
            raise SystemExit("需要 PyYAML 才能读取配置文件：pip install pyyaml")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, data)
    cfg["_path"] = str(p)
    return cfg


def reply_length_hint(cfg):
    """给模型的长度约束说明（中文字符口径）。"""
    hard = cfg["reply"]["hard_limit_units"]
    target = cfg["reply"]["target_chars"]
    return (
        "输出长度硬上限：%d 个汉字以内（数字、字母按半个字算）——"
        "超过这个长度直播间会直接拒收。"
        "建议控制在 %d 个字以内；确实需要把话说清楚时可以适当写长，"
        "但任何时候都不能超过上面的硬上限。" % (hard, target)
    )
