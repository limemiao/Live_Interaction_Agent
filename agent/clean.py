"""文本清洗：繁体转简体、机器人自动回复过滤、Whisper 幻觉过滤、噪声过滤。

设计见 docs/agent_design.md 第 5.1 节。情报库的质量取决于这一层——
把房管脚本和模型幻觉当成人话存进去，后面所有分析都会被污染。
"""

import re

# ---------- 繁体转简体 ----------

_T2S_FALLBACK = {
    "這": "这", "個": "个", "們": "们", "來": "来", "時": "时", "後": "后",
    "裡": "里", "說": "说", "麼": "么", "嗎": "吗", "沒": "没", "會": "会",
    "對": "对", "開": "开", "關": "关", "愛": "爱", "願": "愿", "覺": "觉",
    "現": "现", "實": "实", "還": "还", "邊": "边", "處": "处", "場": "场",
    "長": "长", "門": "门", "間": "间", "題": "题", "發": "发", "話": "话",
    "語": "语", "讀": "读", "寫": "写", "聽": "听", "買": "买", "賣": "卖",
    "錢": "钱", "價": "价", "帶": "带", "點": "点", "見": "见", "頭": "头",
    "麵": "面", "雞": "鸡", "過": "过", "進": "进", "遠": "远", "邊": "边",
    "幾": "几", "週": "周", "歲": "岁", "歲": "岁", "藥": "药", "醫": "医",
    "學": "学", "習": "习", "認": "认", "識": "识", "記": "记", "憶": "忆",
    "聲": "声", "響": "响", "電": "电", "視": "视", "腦": "脑", "車": "车",
    "飛": "飞", "機": "机", "雞": "鸡", "魚": "鱼", "鳥": "鸟", "貓": "猫",
    "狗": "狗", "豬": "猪", "馬": "马", "龍": "龙", "鳳": "凤", "國": "国",
    "軍": "军", "隊": "队", "員": "员", "團": "团", "總": "总", "統": "统",
    "計": "计", "劃": "划", "動": "动", "靜": "静", "氣": "气", "溫": "温",
    "熱": "热", "冷": "冷", "風": "风", "雨": "雨", "雲": "云", "雪": "雪",
    "陽": "阳", "陰": "阴", "區": "区", "號": "号", "線": "线", "網": "网",
    "頁": "页", "書": "书", "報": "报", "紙": "纸", "筆": "笔", "畫": "画",
    "樂": "乐", "戲": "戏", "劇": "剧", "節": "节", "慶": "庆", "賀": "贺",
    "禮": "礼", "費": "费", "銀": "银", "鐵": "铁", "鋼": "钢", "藥": "药",
}


def to_simplified(text: str) -> str:
    """繁体转简体。优先用 opencc，没装则用内置映射表兜底。"""
    if not text:
        return text
    try:
        import opencc

        return opencc.OpenCC("t2s").convert(text)
    except Exception:
        return "".join(_T2S_FALLBACK.get(ch, ch) for ch in text)


# ---------- Whisper 幻觉（字幕套话）----------

HALLUCINATION_RE = re.compile(
    r"不吝点赞|点赞.*订阅.*转发|订阅.*转发.*打赏|打赏支持|明镜与点点"
    r"|明镜需要您的支持|欢迎订阅明|由.{0,6}字幕组|字幕志愿者"
    r"|字幕\s*by|字幕由|字幕组|中文字幕|感谢观看|谢谢观看|我们下期再见|下期再见"
    r"|独播剧场|优优独播|YoYo|Exclusive|MING PAO"
)

# ---------- 房管自动回复脚本 ----------

# 这些是主播账号自动发的互动话术，不是观众说的话，必须剔除
BOT_REPLY_RES = [
    re.compile(r"^感谢.{0,20}(的|了)"),            # 感谢xx的辣条/灯牌/点赞
    re.compile(r"^\d+天(不见|没来)"),                # 3天不见，来得刚刚好
    re.compile(r"记得上次见还是在上次"),
    re.compile(r"是不是偷偷看别的主播"),
    re.compile(r"正缺个互动王"),
    re.compile(r"灯牌亮起来"),
    re.compile(r"要常来看直播哦"),
    re.compile(r"喜欢可以领牌牌"),
    re.compile(r"路过可以，错过"),
    re.compile(r"的辣条，"),
    re.compile(r"^哈喽[~～]?嘘嘘"),
]


def is_bot_reply(text: str) -> bool:
    return (any(r.search(text) for r in BOT_REPLY_RES)
            or any(r.search(text) for r in _EXTRA_PATTERNS))


# 配置文件里追加的模板（昵称/uid 变了也能识别）
_EXTRA_PATTERNS = []


def set_bot_patterns(patterns):
    global _EXTRA_PATTERNS
    _EXTRA_PATTERNS = [re.compile(p) for p in (patterns or [])]


def is_bot_author(uid, uname, cfg=None):
    """按配置里的机器人账号名单判定（uid 或昵称命中即可）。"""
    c = (cfg or {}).get("clean", {})
    if uid and uid in set(c.get("bot_uids") or []):
        return True
    if uname and uname in set(c.get("bot_nicknames") or []):
        return True
    return False


# 开头的 @某某（后面跟空白）——回复弹幕时 B 站客户端会自动加这个前缀，
# 文本里再写一遍就会显示成 @A@A
_LEADING_MENTION = re.compile(r"^@[^\s@]{1,20}\s+")


def strip_leading_mention(text: str) -> str:
    """去掉开头的 @某某，避免与客户端自动添加的前缀重复。"""
    if not text:
        return text
    out = text.strip()
    prev = None
    while prev != out:
        prev = out
        out = _LEADING_MENTION.sub("", out).strip()
    return out


def is_noise_text(text: str) -> str:
    """判断一条已入库文本是不是"该标为噪声"的内容。返回原因，正常则空串。

    场景：早期导入的数据没经过现在的过滤器（幻觉黑名单、唱歌判定），
    里面混着 Whisper 幻觉和唱歌糊掉的句子。这些进知识库会污染检索。
    """
    t = (text or "").strip()
    if not t:
        return "empty"
    if HALLUCINATION_RE.search(t):
        return "whisper_hallucination"
    # 纯英文长句 -> 歌词（与 ASR 侧同一条规则）
    letters = sum(1 for c in t if c.isascii() and c.isalpha())
    if letters >= 6 and letters / len(t) > 0.5:
        return "english_lyrics"
    # 同一句在库里重复出现多次 -> 复读/幻觉循环
    return ""


# ---------- 噪声 ----------

_EMOJI_ONLY = re.compile(r"^(?:\[[^\]]{1,20}\])+$")


def is_noise(text: str) -> bool:
    """纯表情、单字、纯标点。"""
    t = text.strip()
    if len(t) <= 1:
        return True
    if _EMOJI_ONLY.match(t):
        return True
    if not re.search(r"[\w\u4e00-\u9fff]", t):
        return True
    return False


# ---------- 统一入口 ----------

def normalize_text(text: str, kind: str = "danmaku", is_bot: bool = False):
    """清洗一条文本。返回 (清洗后文本, 丢弃原因)；未丢弃时原因为 None。

    is_bot=True 表示这条已判定来自机器人账号：照常保留内容（打标入库），
    但跳过"机器人话术/噪声"这两项拒绝逻辑——因为它本来就是机器人说的。
    """
    if not text:
        return "", "empty"
    t = text.strip()

    if kind == "speech":
        if HALLUCINATION_RE.search(t):
            return "", "whisper_hallucination"
        t = to_simplified(t)
        # 术语纠错：ASR 的同音错字（舰长→肩章 等）
        from .lexicon import correct_terms
        t, _ = correct_terms(t)
    else:
        if not is_bot:
            if is_bot_reply(t):
                return "", "bot_reply"
            if is_noise(t):
                return "", "noise"
        t = to_simplified(t)

    return t, None


class OwnEcho:
    """识别"这条弹幕其实就是机器人自己刚发的那句"。

    为什么不能只靠 uid / 昵称：未开播的房间（以及偶发的打码投递）里，
    B 站会把弹幕的用户信息抹成 `uid=0` + 昵称打码（`序***`），
    机器人于是认不出自己的话——实测出现过它回复自己上一条弹幕，
    而它那条回复里正好含自己的名字，就会继续被自己触发。

    文本 + 时间窗口不依赖任何用户字段：发送前先登记，收到同文本的弹幕
    就判为自己发的。发送失败也照样登记（宁可少回一条，不要自问自答）。
    """

    def __init__(self, window_s: float = 180, keep: int = 100):
        self.window_s = window_s
        self.keep = keep
        self._sent = []          # [(归一的文本, 毫秒时间戳)]

    @staticmethod
    def _key(text):
        # 忽略空白差异：WS 回显和模型输出在空格/换行上偶尔不一致
        return "".join((text or "").split())

    def note(self, text, ts_ms):
        """发送前登记这条内容。"""
        k = self._key(text)
        if not k:
            return
        self._sent.append((k, int(ts_ms)))
        if len(self._sent) > self.keep:
            self._sent = self._sent[-self.keep:]

    def is_echo(self, text, ts_ms, window_s=None):
        """这条弹幕是不是自己刚发的？"""
        k = self._key(text)
        if not k:
            return False
        win = float(window_s if window_s is not None else self.window_s) * 1000
        for t, ts in reversed(self._sent):
            if t != k:
                continue
            delta = ts_ms - ts
            # 下界要留一点负数余量：弹幕日志的时间戳只到秒（HH:MM:SS），
            # 而登记用的是毫秒。同一秒内"先记后发"的消息，回显解析出来
            # 反而比登记时刻早最多 1 秒——不留余量这功能在生产上等于失效。
            if -2000 <= delta <= win:
                return True
        return False
