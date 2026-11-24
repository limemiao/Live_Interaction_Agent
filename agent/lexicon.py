"""直播领域词表与转写纠错。

Whisper 对直播黑话误识别很有规律——同音字替换：
    舰长 → 肩章 / 尖掌 / 尖长
    人气票 → 年气票 / 连气票
    示例主播 → 虚虚念 / 细细念
    主播 → 虚虚 / 嘘嘘 / 细细 / 西西

两条路一起用：
  1) 解码偏置：把词表塞进 Whisper 的 initial_prompt，让它一开始就往对的方向听
  2) 事后纠错：按下表把已知错词替换回来（对已入库的老数据同样有效）
"""

import re

# 领域词表：既用于解码偏置，也用于模糊纠错的候选集
DOMAIN_TERMS = [
    "舰长", "提督", "总督", "大航海", "上舰", "续舰", "掉舰",
    "粉丝牌", "灯牌", "卡灯牌", "粉丝团", "人气票", "辣条",
    "醒目留言", "上电视", "连麦", "上麦", "PK", "打call",
    "示例主播", "主播", "嘘嘘", "粉丝团", "直播间", "弹幕",
    "投稿", "回放", "关注", "充电", "舰长群",
    "小助手",
]

# 「小助手」三个字都是易混音节，Whisper 常听成这些。
# 名字跟其他词不一样——它是**触发条件**，错一个就少回一次，
# 所以既要在纠错表里改回来，也要让触发判定自己认这些变体。
NAME_VARIANTS = {
    "小助手": ["小小絮", "小小许", "小小旭", "小小玉", "小小须", "小小续",
               "小序", "小小旭", "小小絮", "肖肖序", "小晓序", "小小徐",
               "笑笑序", "小小絮序"],
}

# 已知误识别 -> 正确写法（从真实语料里收集的）
CORRECTIONS = {
    "肩章": "舰长", "尖掌": "舰长", "尖长": "舰长", "剑掌": "舰长",
    "见长": "舰长", "键长": "舰长", "煎掌": "舰长", "肩长": "舰长",
    "舰掌": "舰长", "剑长": "舰长",
    "年气票": "人气票", "连气票": "人气票", "人汽票": "人气票",
    "虚虚念": "示例主播", "细细念": "示例主播", "絮絮念": "示例主播",
    "嘘嘘念": "示例主播", "徐徐念": "示例主播",
    "虚虚": "主播", "嘘嘘": "主播", "细细": "主播", "西西": "主播",
    "洗洗": "主播", "须须": "主播",
    "灯牌": "灯牌", "登牌": "灯牌",
    "粉丝盘": "粉丝牌", "粉丝排": "粉丝牌",
    "上见": "上舰", "上剑": "上舰",
    "连卖": "连麦", "连脉": "连麦",
}

# 名字变体并入纠错表（必须在 _PATTERN 之前完成）
for _canon, _vs in NAME_VARIANTS.items():
    for _v in _vs:
        CORRECTIONS.setdefault(_v, _canon)

_PATTERN = re.compile("|".join(re.escape(k) for k in CORRECTIONS))


def asr_initial_prompt():
    """给 Whisper 的解码偏置提示词。

    Whisper 会把这段当成"前文"来续写，因此出现在这里的词会被优先输出。
    注意别写太长，过长会挤占上下文。
    """
    return ("以下是B站直播间的普通话对话。常见词：舰长、提督、总督、粉丝牌、"
            "灯牌、人气票、辣条、醒目留言、连麦、上舰、示例主播、主播、粉丝团、"
            "小助手。")


def name_variants(names):
    """把一个人名扩展成"可能的误识别写法"集合，供触发判定使用。"""
    out = set()
    for n in names or []:
        out.add(n)
        for v in NAME_VARIANTS.get(n, []):
            out.add(v)
    return out


def correct_terms(text):
    """按错词表纠错。返回 (纠正后文本, 纠正次数)。"""
    if not text:
        return text, 0
    n = 0

    def _sub(m):
        nonlocal n
        n += 1
        return CORRECTIONS[m.group(0)]

    out = _PATTERN.sub(_sub, text)
    return out, n


def fuzzy_correct(text, max_dist=1):
    """对词表做模糊匹配，纠正没收录的近似错词。

    只对长度 2~3 的词做，且要求只差一个字——再宽就会误伤正常用词。

    **注意：默认不启用。** 实测会过度纠正（"连脉连一个" → "连麦连麦个"，
    把正常的"一个"也改坏了）。静默改写正常内容比漏纠错更危险，
    所以只保留人工整理的 CORRECTIONS 表。这个函数留作实验用。
    """
    if not text:
        return text, 0
    terms = [t for t in DOMAIN_TERMS if len(t) >= 2]
    fixed = 0
    # 按 2~3 字窗口滑动，命中候选才替换
    for size in (3, 2):
        i = 0
        out = []
        while i < len(text):
            w = text[i:i + size]
            if len(w) == size and not w.isascii():
                best, best_d = None, 99
                for t in terms:
                    if len(t) != size:
                        continue
                    d = sum(1 for a, b in zip(w, t) if a != b)
                    if d < best_d:
                        best, best_d = t, d
                if best and 0 < best_d <= max_dist:
                    out.append(best)
                    fixed += 1
                    i += size
                    continue
            out.append(text[i])
            i += 1
        text = "".join(out)
    return text, fixed
