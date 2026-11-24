"""护栏：发送前的硬约束校验。

设计见 docs/agent_design.md 第 6.4 节。这些是代码级约束，
不依赖模型自觉——模型说什么都得先过这一关。
"""

import difflib
import re

# 实测得出的长度规则：汉字算 1、英文字母算 0.5，上限 40 单位。
# 依据：40 汉字（120 字节）能过，119 个字母（119 字节）被拒，见研究记录 2.4 节。
MAX_UNITS = 40.0


def weighted_len(text: str) -> float:
    """按 B 站实测规则计算长度权重。"""
    units = 0.0
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF:
            units += 1.0        # 汉字 / 日文假名
        elif ch.isascii():
            units += 0.5        # 字母数字标点
        else:
            units += 1.0        # 全角符号、emoji 等，按 1 保守计
    return units


BANNED_WORDS = ["加群", "私聊", "微信", "QQ群", "代刷", "代充",
                "返利", "点击链接", "http://", "https://"]
BANNED_RE = re.compile("|".join(re.escape(w) for w in BANNED_WORDS))

# 机器人不该说的话：暴露身份
IDENTITY_WORDS = ["我是机器人", "我是AI", "我是人工智能", "我是助手",
                  "系统提示", "作为一个AI", "作为一个人工智能",
                  "我是程序", "我是模型", "我是语言模型", "作为程序",
                  "作为模型", "我是虚拟主播", "我是虚拟", "我被设定"]
IDENTITY_RE = re.compile("|".join(re.escape(w) for w in IDENTITY_WORDS))


def check_length(text: str):
    u = weighted_len(text)
    if u > MAX_UNITS:
        return False, "长度超限 %.1f/%.0f 单位" % (u, MAX_UNITS)
    return True, None


def check_banned(text: str):
    if BANNED_RE.search(text):
        return False, "命中屏蔽词"
    return True, None


def check_identity(text: str):
    if IDENTITY_RE.search(text):
        return False, "暴露机器人身份"
    return True, None


def check_cooldown(last_sent_ts, now_ts, min_interval_s=30):
    if last_sent_ts is None:
        return True, None
    gap = (now_ts - last_sent_ts) / 1000.0
    if gap < min_interval_s:
        return False, "距上次发言仅 %.0fs（下限 %ds）" % (gap, min_interval_s)
    return True, None


def check_rate(sent_history_ts, now_ts, per_minute=1, per_hour=20):
    """sent_history_ts: 最近发送时间戳列表（毫秒）。"""
    in_min = [t for t in sent_history_ts if now_ts - t < 60000]
    if len(in_min) >= per_minute:
        return False, "本分钟已发 %d 条（上限 %d）" % (len(in_min), per_minute)
    in_hour = [t for t in sent_history_ts if now_ts - t < 3600000]
    if len(in_hour) >= per_hour:
        return False, "本小时已发 %d 条（上限 %d）" % (len(in_hour), per_hour)
    return True, None


def check_duplicate(text: str, recent_texts, threshold=0.8):
    for prev in recent_texts or []:
        ratio = difflib.SequenceMatcher(None, text, prev).ratio()
        if ratio > threshold:
            return False, "与最近发言重复度过高（%.2f）" % ratio
    return True, None


def evaluate(text, now_ts, recent_texts=None, duplicate_threshold=0.8):
    """内容级护栏，返回 (是否通过, 拒绝原因)。

    频率与间隔不在这里判定——那些交给发送队列处理：队列是"排队等待"，
    而护栏是"直接否决"。如果在这里因为发得太快而拒绝，消息会直接丢掉，
    而正确行为是排队等一会儿再发。
    """
    checks = [
        (check_length, (text,)),
        (check_banned, (text,)),
        (check_identity, (text,)),
        (check_duplicate, (text, recent_texts or [], duplicate_threshold)),
    ]
    for fn, args in checks:
        ok, reason = fn(*args)
        if not ok:
            return False, reason
    return True, None


# 机器人自己的账号名（写记忆时用来识别"这条在描述我自己是什么"）
SELF_NAMES = ("小助手", "示例机器人", "示例版")
# 一旦和上面的名字同现，就属于"把自己说成程序"的内容
IDENTITY_CLAIM_WORDS = ("AI", "ai", "人工智能", "程序", "机器人", "助手",
                        "智能", "模型", "gpt", "GPT", "脚本", "bot", "BOT")


def looks_like_self_identity(text):
    """这段内容是不是在说"小助手是 AI/程序/机器人"？

    为什么要代码级拦：这类内容一旦写进 memories，会被当作"房间事实"注入
    提示词，和身份底线（不承认自己是 AI）直接冲突——模型看到"小助手=AI助手"
    这条"事实"，很容易顺着说起自己是助手。实测抽取器已经写出过一条。
    底线不靠模型自觉，写入前就挡掉。
    """
    t = text or ""
    if not any(n in t for n in SELF_NAMES):
        return False
    return any(w in t for w in IDENTITY_CLAIM_WORDS)


# 发送接口返回码 → 人话（执行层记录失败原因用）
ERROR_CODES = {
    0: "成功",
    -101: "未登录",
    -111: "csrf 校验失败",
    -400: "参数错误",
    -403: "权限不足（可能被禁言）",
    10030: "发送频率过快或被限制",
    1003212: "超出限制长度",
}
