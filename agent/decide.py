"""决策管线：触发判定 → 上下文组装 → LLM 生成 → 护栏校验 → 落 decisions 表。

默认 **影子模式**：生成候选但一条都不发。设计见 docs/agent_design.md 第 6 章。
"""

import json
import re
import time

from . import clean, guard
from . import tools as tools_mod
from .platform import PLATFORM_KNOWLEDGE
from .personas import DEFAULT_PERSONA, full_system_prompt

# 机器人的身份设定。真实使用时按你的目标改写这一段。
DEFAULT_PERSONA = (
    "你是一个 B 站直播间的老观众，不是助手、不是客服。"
    "说话简短、口语化，像弹幕一样自然，可以带一点点网感。"
    "不要用书面语，不要列点，不要解释自己。"
    "不确定的事就说不知道或直接岔开，绝不编造。"
)

# 触发用的关键词（可被 --keywords 覆盖）
DEFAULT_KEYWORDS = ["示例主播", "嘘嘘", "主播"]

QUESTION_RE = re.compile(r"[?？]$|吗[?？]?$|呢[?？]?$|怎么|为啥|为什么|是不是|有没有")


def detect_trigger(event, bot_name="", keywords=None, trigger_types=("danmaku",),
                   use_question_regex=True, names=None, operator_uid=0):
    """判断一条事件是否该触发回复。返回 (trigger, target_uid, target_uname) 或 None。

    use_question_regex=False 时关闭"问句自动触发"，只保留 @提及 / 关键词 / 礼物。
    测试阶段建议关掉，让触发时机完全可控。
    """
    if event.get("is_bot"):
        return None            # 不对机器人说话
    etype = event.get("type")
    if etype not in trigger_types:
        return None
    text = event.get("text") or ""
    uid = event.get("uid")
    uname = event.get("uname")

    # 叫名字就应答：主播说到「小助手」或弹幕里出现「小助手」
    for nm in (names or []):
        if nm and nm in text:
            return ("anchor_call" if etype == "speech" else "called"), uid, uname

    # 主播语音只在叫到名字时才触发——否则每句话都会触发一次
    if etype == "speech":
        return None

    if etype == "gift":
        return "gift", uid, uname
    if etype == "superchat":
        return "superchat", uid, uname

    if bot_name and ("@" + bot_name) in text:
        return "mention", uid, uname
    # 关键：B 站客户端回复弹幕时文本里不含 @（@ 由客户端根据 reply_mid 渲染），
    # 所以要靠回复元数据判断，否则用客户端回复永远触发不了。
    meta = event.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    reply_uname = str(meta.get("reply_uname") or "").strip()
    if bot_name and reply_uname and reply_uname == bot_name:
        return "mention", uid, uname
    # 指令触发：命中"叫我/记住/换成/拉黑"这类词 **且确实是在对机器人说话**。
    # 光有词不算数：直播间里"拉黑了""回来忘了"随手就说，
    # 不限制的话机器人会对每条这样的弹幕都回一句——实测：
    #   tonyliu23「吓我一跳，拉黑了[囧]」→ 机器人回「别怕呀～我可舍不得拉黑你」
    #   「别叫我了求你了」→ 命中"叫我"；「结果回来忘了」→ 命中"忘了"
    # 名字/@/客户端回复这三种"确实在叫机器人"的情况在上面的分支已经返回，
    # 所以走到这里还能算"对我说话"的只剩操作者本人的指令。
    is_op = bool(uid) and uid == operator_uid
    if etype == "danmaku" and is_op and tools_mod.should_judge(text,
                                                              is_operator=True):
        return "command", uid, uname
    low = text.lower()
    for kw in (keywords or []):
        if kw and kw.lower() in low:
            return "keyword", uid, uname
    if use_question_regex and QUESTION_RE.search(text):
        return "question", uid, uname
    return None


def build_query(trigger_text, recent_lines, max_recent=5):
    """构造检索查询。

    原来的做法是取触发文本的前 8 个字——口语短句这么切几乎命中不了任何东西。
    正确做法是：触发文本 + 最近几条上下文，让语义检索有足够信息。
    """
    parts = [trigger_text or ""]
    parts += [l.split(": ", 1)[-1] for l in (recent_lines or [])[-max_recent:]]
    return " ".join(p for p in parts if p)[:200]


def build_context(store, session_id, now_ts, window_s=180, max_lines=25,
                  room_id=None, target_uid=None, trigger_text=None,
                  index=None, top_k=6, min_score=0.55, bot_uid=None):
    """组装上下文：近场逐字 + 知识库 + 语义检索到的历史。"""
    # 连自己刚发的弹幕一起取：有人用客户端回复你（"你刚才说啥"）时，
    # 看不到自己上一句就只能瞎猜。别人的机器人（房管脚本）不进上下文——
    # 那些是模板话术，混进来会把模型带跑。
    events = store.recent_events(session_id, before_ts=now_ts,
                                 window_s=window_s, include_bot=True)
    danmaku, speech = [], []
    for e in events:
        # 带上 HH:MM：模型需要知道每句话是什么时候说的，
        # 才能判断"主播刚刚说的"和"三分钟前说的"哪个更近。
        hm = time.strftime("%H:%M", time.localtime(e["ts"] / 1000))
        if e["type"] == "speech":
            speech.append("[语音 %s] %s" % (hm, e["text"] or ""))
        elif e["type"] == "danmaku":
            if e.get("is_bot"):
                if not bot_uid or e.get("uid") != bot_uid:
                    continue          # 别人的机器人/房管脚本，照旧不进上下文
                danmaku.append("[我 %s] 我上一句: %s" % (hm, e["text"] or ""))
            else:
                danmaku.append("[danmaku %s] %s: %s" % (hm, e["uname"] or "?",
                                                        e["text"] or ""))
    ctx = {
        "danmaku": danmaku[-max_lines:],
        "speech": speech[-max_lines:],
        "event_count": len(events),
        "facts": None, "person": None, "summary": None, "history": [],
    }
    # 知识库：事实卡（常驻）+ 本场滚动摘要
    if room_id:
        f = store.kb_facts(room_id)
        if f:
            try:
                ctx["facts"] = json.loads(f["content"])
            except Exception:
                ctx["facts"] = None
        # 本场话题（工具链写的，键里带 session_id，不会串到往期）
        ctx["topic"] = store.session_topic(session_id, room_id)
    s = store.session_summary(session_id)
    if s:
        ctx["summary"] = s["content"]
        ctx["summary_through"] = s.get("through_ts")
    # 人物卡：当前交互对象
    if target_uid:
        ctx["person"] = store.kb_person(target_uid)
    # 语义检索：找相关的历史（排除本场，避免只是复读刚刚的话）
    if index is not None and trigger_text:
        q = build_query(trigger_text, danmaku)
        try:
            ctx["history"] = index.search(q, top_k=top_k,
                                          exclude_session=session_id,
                                          min_score=min_score)
        except Exception as exc:
            ctx["history"] = []
            ctx["history_error"] = str(exc)[:100]
    return ctx


def render_messages(persona, context, trigger, target_uname, user_profile,
                    history_hits=None, length_hint=None,
                    focus_text=None, max_units=guard.MAX_UNITS,
                    focus_type="danmaku"):
    """把上下文渲染成 chat messages。

    **静态内容全部放 system、动态内容放 user**——DeepSeek 的前缀缓存
    按消息前缀命中，system 稳定就能一直命中，省掉重复计费。

    focus_type 说明"要回应的那条"在哪：danmaku=最近弹幕里那一条，
    speech=主播刚说的话。不区分的话，语音触发时提示词里一条 >>> 都没有，
    模型却被告知"只回应标注 >>> 的那条弹幕"，只能瞎挑。
    """
    # ===== 静态部分（system，尽量不变）=====
    static = [persona, "", PLATFORM_KNOWLEDGE]
    facts = context.get("facts")
    if facts:
        lines = []
        for k, label in [("streamer_names", "称呼"), ("identity", "身份"),
                         ("topics", "常聊"), ("catchphrases", "口头禅"),
                         ("slang", "房间黑话"), ("audience_style", "观众风格"),
                         ("avoid", "避免")]:
            v = facts.get(k)
            if not v:
                continue
            if isinstance(v, list):
                v = "、".join(str(x) for x in v)
            elif isinstance(v, dict):
                v = "；".join("%s=%s" % (a, b) for a, b in v.items())
            lines.append("  %s: %s" % (label, v))
        if lines:
            static.append("")
            static.append("【这个直播间的基本情况】")
            static.extend(lines)
    static.append("")
    if length_hint:
        static.append(length_hint)
    static.append("不要写 @某某 前缀——系统会自动带上被回复人的名字，"
                  "你再写一遍就会显示成两个 @。")
    static.append("")
    static.append("【输出格式】只输出你要发的那一句话本身，"
                  "不要 JSON、不要解释、不要前缀或引号。")

    # ===== 动态部分（user，每次不同）=====
    parts = []

    # ---- 知识库：当前交互对象的人物卡 ----
    person = context.get("person")
    if person and (person.get("summary") or person.get("style")):
        parts.append("【你正在回复的这个人】")
        parts.append("  昵称: %s（发言 %d 次）" % (person.get("uname") or "?",
                                                 person.get("msg_count") or 0))
        if person.get("summary"):
            parts.append("  印象: %s" % person["summary"])
        if person.get("topics"):
            parts.append("  常聊: %s" % person["topics"])
        if person.get("style"):
            parts.append("  风格: %s" % person["style"])
        if person.get("quote"):
            parts.append("  他说过: %s" % person["quote"])
        if person.get("updated_at"):
            # 印象是离线归纳的，可能过时——告诉模型归纳时间，
            # 它才知道该不该把"他最近在考研"当成仍然成立的事。
            parts.append("  （以上印象归纳于 %s，可能已过时）"
                         % str(person["updated_at"])[:10])
        parts.append("")

    # ---- 工具链写入的记忆:别名、备注、偏好 ----
    # memories_for 只带回该用户的记录和房间级记录(slang:/fact:/topic:),
    # 这里只注入属于"这个人"的(alias/note/pref),避免把内部状态泄进提示词。
    mem = context.get("memories") or []
    mem_lines, fact_lines = [], []
    for m in mem:
        k, v = str(m.get("key") or ""), str(m.get("value") or "")
        mid = int(m.get("id") or 0)      # 自增 id = 稳定的写入先后
        # 记录时间让模型能判断新旧:同一周记的和半年前记的,可信度不同
        ca = str(m.get("created_at") or "")
        when = "（记于 %s）" % ca[5:10] if len(ca) >= 10 else ""
        if k == "alias":
            mem_lines.append("  ta 希望你叫 ta:%s%s" % (v, when))
        elif k.startswith("note:"):
            mem_lines.append("  关于 ta:%s%s" % (v, when))
        elif k.startswith("pref:"):
            mem_lines.append("  ta 的偏好(%s):%s%s" % (k[5:], v, when))
        elif k.startswith("fact:"):
            # 房间级记录（主播亲口说的）：以前写完就没人读，现在注入。
            fact_lines.append((mid, "  %s：%s%s" % (k[5:], v, when)))
        elif k.startswith("slang:"):
            fact_lines.append((mid, "  黑话「%s」= %s%s" % (k[6:], v, when)))
    if mem_lines:
        parts.append("【你记住的关于 ta 的事】")
        parts.extend(mem_lines[:6])
        parts.append("  (自然地使用这些信息,不要逐条念出来)")
        parts.append("")

    if fact_lines:
        # 新的在前：越旧的事实越可能已经过时（"明天不播"过期就是错的），
        # 所以宁可留最近几条，也不把最早记的那条一直摆在最上面。
        # 按自增 id 排，不按 created_at——后者只到秒，同一秒写的会乱序。
        fact_lines.sort(key=lambda t: t[0], reverse=True)
        parts.append("【你记下的这个直播间的事】（主播亲口说的）")
        parts.extend(x for _, x in fact_lines[:8])
        if len(fact_lines) > 8:
            parts.append("  （另有 %d 条更早的没列出来）" % (len(fact_lines) - 8))
        parts.append("  过时的就不要再引用；拿不准的时候按最新的一条说。")
        parts.append("")

    # ---- 本场正在聊的话题（工具链写的）----
    if context.get("topic"):
        parts.append("【本场正在聊】%s" % context["topic"])
        parts.append("")

    # ---- 本场早前的摘要（解决"只记得最后 3 分钟"）----
    if context.get("summary"):
        head = "【本场前面聊过的】"
        if context.get("summary_through"):
            head += "（摘要截至 %s）" % time.strftime(
                "%H:%M", time.localtime(context["summary_through"] / 1000))
        parts.append(head)
        parts.append("  " + context["summary"].replace("\n", "\n  "))
        parts.append("")

    # ---- 检索到的历史（带出处）----
    if context.get("tool_note"):
        parts.append("【你刚才做过的事】")
        parts.append("  " + context["tool_note"])
        parts.append("")

    hist = context.get("history") or []
    if hist:
        parts.append("【相关的其他场次内容】（引用时以这些为准，不要编造）")
        # 条数由检索侧的 top_k 控制，这里不再二次截断——
        # 否则检索取 6 条、只渲染 5 条，白浪费一个名额。
        for h in hist:
            who = h.get("uname") or "观众"
            tag = "主播" if h.get("kind") == "speech" else who
            # 带出处：设计文档要求检索结果必须能追溯，否则模型会把
            # 检索到的内容当成自己的记忆瞎编。日期 + 发言人是最小出处。
            when = ""
            if h.get("ts"):
                when = time.strftime("%m-%d %H:%M",
                                     time.localtime(h["ts"] / 1000))
            parts.append("  [%s %s] %s" % (when, tag, (h.get("text") or "")[:60]))
        parts.append("")

    if context["danmaku"]:
        parts.append("最近弹幕（标了 >>> 的那条才是你要回应的；"
                     "以 [我] 开头的是你自己刚发过的话，不要回它）：")
        for line in context["danmaku"]:
            if focus_text and focus_text[:20] in line:
                parts.append("  >>> " + line)
            else:
                parts.append("  " + line)
    if context["speech"]:
        parts.append("")
        parts.append("主播最近说的话（语音转写，可能有同音错别字，"
                     "明显不通顺的句子多半是识别糊了，不要引用）：")
        for line in context["speech"]:
            # 语音触发时焦点在这里：主播叫了你的名字那句话
            if (focus_type == "speech" and focus_text
                    and focus_text[:20] in line):
                parts.append("  >>> " + line)
            else:
                parts.append("  " + line)
    if history_hits:
        parts.append("")
        parts.append("相关历史：")
        for h in history_hits[:3]:
            parts.append("  %s" % (h.get("text") or "")[:60])
    if user_profile:
        parts.append("")
        parts.append("对方信息：%s，来本直播间 %d 次，粉丝牌 %s lv%d" % (
            user_profile.get("uname") or "?",
            user_profile.get("msg_count") or 0,
            user_profile.get("medal_name") or "无",
            user_profile.get("medal_level") or 0))

    task = {
        "mention": "有人 @ 你，回复 ta。",
        "called": "有人在弹幕里叫了你的名字，回一句。",
        "keyword": "有人提到了关键词，接一句话。",
        "question": "有人在问问题，如果知道就简短回答，不知道就带过。",
        "gift": "有人送了礼物，道个谢。",
        "superchat": "有人发了醒目留言，认真回应一下。",
        "anchor_call": "主播刚叫了你的名字，自然接一句。",
        "command": "有人在对你说事情，按上面的情况回应。",
    }.get(trigger, "接一句话。")
    parts.append("")
    parts.append("任务：%s" % task)
    if focus_type == "speech":
        parts.append("只回应标注 >>> 的那句主播的话——那是主播对你说的。"
                     "其他弹幕只是背景，不要回答它们提到的问题。")
    else:
        parts.append("只回应标注 >>> 的那一条弹幕。其他弹幕只是背景，"
                     "不要回答它们提到的问题。")
    if context.get("person") or context.get("history"):
        parts.append("如果【你正在回复的这个人】或【相关的其他场次内容】里有"
                     "跟当前话题相关的东西，自然地带一句，例如"
                     "「你上次不是说…」。但不要每句都引用，也不要为了引用而引用。")
        parts.append("只允许引用上面明确写出来的内容，绝对不要把没写出来的"
                     "当成事实说出来。")
    return [
        {"role": "system", "content": "\n".join(static)},
        {"role": "user", "content": "\n".join(parts)},
    ]


class SendState:
    """模拟"已发送"状态，让护栏在影子/回放模式下也能按真实节奏生效。"""

    def __init__(self):
        self.last_ts = None
        self.history_ts = []
        self.recent_texts = []
        # 真正发出去的文本（手动 + 自动）。手动发送的去重基线用它，
        # **不能用 recent_texts**——那里面装的是"生成过的候选"，
        # 影子模式下一条都没发出去，却会把用户手打的内容判成重复。
        self.sent_texts = []
        # 同一个人重复触发的时间戳：5 秒内只回第一条
        self.last_trigger_ts = {}

    def accept(self, ts, text):
        self.last_ts = ts
        self.history_ts.append(ts)
        self.recent_texts.append(text)
        if len(self.recent_texts) > 20:
            self.recent_texts = self.recent_texts[-20:]

    def note_sent(self, text, ts=None):
        """记下一条"确实发出去了"的内容（供排查/统计用）。

        注意：手动发送**不再用它做去重门禁**——人重复说是正常行为，
        去重只用于机器人自己的自动回复（那走 recent_texts）。
        """
        text = (text or "").strip()
        if not text:
            return
        self.sent_texts.append((text, ts if ts is not None else time.time()))
        if len(self.sent_texts) > 50:
            self.sent_texts = self.sent_texts[-50:]


def decide_one(store, llm, session_id, event, cfg=None, persona=None,
               bot_name=None, keywords=None, state=None, window_s=180,
               dry_run=True, min_interval_s=None, index=None, room_id=None):
    """对单条事件跑一次完整决策。dry_run=True 时只记录不发送。"""
    from .config import reply_length_hint

    cfg = cfg or {}
    rcfg = cfg.get("reply") or {}
    tcfg = cfg.get("triggers") or {}
    room_id = room_id or (event.get("room_id"))
    if not room_id and session_id:
        # 调用方忘了传就自己查。room_id 决定记忆落在哪个桶、以及"房间事实 /
        # 本场话题"取不取得到——静默取不到是很难发现的 bug（回归里踩过一次：
        # 实时链路没传 room_id，记忆区段整个消失）。
        room_id = store.session_room(session_id)
    bot_name = bot_name if bot_name is not None else (rcfg.get("bot_name") or "")
    keywords = keywords if keywords is not None else (tcfg.get("keywords") or [])
    trigger_types = tuple(tcfg.get("types") or ("danmaku",))
    use_question_regex = bool(tcfg.get("question_regex", True))
    names = tcfg.get("names") or []
    # 名字可能被识别错（小助手 → 小小絮/小小许…），触发判定要容忍变体，
    # 否则纠错漏一个就少回一次
    from .lexicon import name_variants
    names = list(name_variants(names))
    trigger_cooldown_s = float(tcfg.get("trigger_cooldown_s", 5))
    if min_interval_s is None:
        min_interval_s = (cfg.get("guard") or {}).get("min_interval_s", 30)
    # 机器人自己的 uid：用来把"我上一句"跟别人的话区分开
    # （房管脚本也是 is_bot，但不能当成"我说的"）
    bot_uid = rcfg.get("bot_uid")
    try:
        bot_uid = int(bot_uid) if bot_uid else None
    except (TypeError, ValueError):
        bot_uid = None

    # 机器人层面忽略：被忽略的人发的弹幕不触发（连模型都不调用）
    if store.is_ignored(event.get("uid"), room_id):
        return None

    trig = detect_trigger(event, bot_name=bot_name, keywords=keywords,
                          trigger_types=trigger_types,
                          use_question_regex=use_question_regex,
                          names=names,
                          operator_uid=(cfg.get("operator_uid") or 0))
    if not trig:
        return None
    trigger, target_uid, target_uname = trig

    state = state or SendState()
    # 同一个人短时间内重复触发，只回第一条。
    # 主播语音没有 uid（不是"谁"在说话），按房间算一条冷却——主播连喊几声
    # 名字不该换来一串回复，那正是最容易触发发送频率风控的情形。
    is_anchor_src = event.get("type") == "speech"
    # 冷却键：有真实 uid 就按人算；主播语音、以及 uid 被抹成 0 的"未知来源"
    # 弹幕（未开播的房间会这样投递），统一按房间算一条冷却。
    # 原来 uid=0 会落到 key=0，而下面 `if key` 把 0 当假值 → 完全没有冷却。
    key = target_uid if target_uid else -1
    if key and state.last_trigger_ts.get(key, 0) + trigger_cooldown_s * 1000 > event["ts"]:
        return None
    state.last_trigger_ts[key] = event["ts"]

    now_ts = event["ts"]

    profile = store.user_profile(target_uid) if target_uid else None
    text = event.get("text") or ""
    ccfg = cfg.get("context") or {}
    ctx = build_context(store, session_id, now_ts, window_s=window_s,
                        room_id=room_id, target_uid=target_uid,
                        trigger_text=text, index=index,
                        top_k=ccfg.get("history_top_k", 6),
                        min_score=ccfg.get("min_score", 0.55),
                        bot_uid=bot_uid)
    # 记忆（工具链写入的）：本人的别名/备注/偏好 + 房间级事实/黑话。
    # target_uid 为空时也要取：主播语音触发的那一轮没有"对方"，
    # 但"直播间事实"恰恰是它最该看到的（那些是 B 档工具写下来的）。
    # uid=None 传给 SQL 不会匹配任何用户行，只会带回房间级记录。
    mem = store.memories_for(target_uid, room_id) if room_id else []
    if mem:
        ctx["memories"] = mem

    # ---- 第一步：先判断要不要调工具，执行完再生成回复 ----
    # 单独做这一步是因为让模型"一边说话一边记得写 JSON"很不可靠：
    # 实测它会把指令当玩笑，完全不出 actions。
    tool_note = None
    tool_results = None
    tool_ids = []
    is_op = bool(target_uid) and target_uid == cfg.get("operator_uid")
    # 语音事件不走关键词预筛：主播"陈述事实"类意图词表覆盖不到，
    # 且 anchor_call 本来就稀少（叫到名字才触发），直接判定成本可控。
    if is_anchor_src or tools_mod.should_judge(text, is_operator=is_op):
        judged = tools_mod.judge(
            llm, text, target_uid, target_uname, ctx.get("danmaku"),
            is_operator=is_op, is_anchor=is_anchor_src)
        if judged:
            # 一次可以执行多条：多意图的话（"叫我大王，顺便记住我是考研的"）
            # 不该只执行第一条
            calls = [dict(a, tool=t) for t, a in judged]
            tool_results = tools_mod.execute(
                store, calls,
                {"uid": target_uid, "uname": target_uname,
                 "operator_uid": (cfg.get("operator_uid") or 0),
                 "room_id": room_id, "session_id": session_id,
                 "source_text": text,
                 "source_is_anchor": is_anchor_src,
                 "decision_id": None})
            # 工具先执行、决策后落库，所以这里只能先记下行号，等决策拿到 id 再回填
            tool_ids = [r["id"] for r in tool_results if r.get("id")]
            oks = [r["tool"] for r in tool_results if r.get("status") == "ok"]
            bad = [r for r in tool_results if r.get("status") != "ok"]
            notes = []
            if oks:
                notes.append("你刚刚已经执行了：" + "、".join(oks)
                             + "。可以自然地提一句，也可以不提。")
            if bad:
                notes.append("对方要求你执行 %s，但被拒绝了（%s）。"
                             "用轻松的方式说明你做不到或不愿意，不要解释规则。"
                             % (bad[0]["tool"], bad[0].get("reason") or ""))
            tool_note = " ".join(notes) if notes else None
            ctx["tool_note"] = tool_note
    # 人设在这里才读：工具可能刚执行过 switch_persona，读完再渲染，
    # 操作者切换人设的那一条回复就会用新语气（否则要等下一条，看起来像没生效）。
    # config.reply.persona 作为「普通观众」的风格覆盖；显式传入的 persona
    # 参数优先。身份与不可变底线仍由 personas.py 提供，配置改不了。
    persona_override = (persona if persona is not None
                        else (rcfg.get("persona") or ""))
    sys_prompt = full_system_prompt(
        store.get_setting("persona", DEFAULT_PERSONA),
        normal_override=persona_override)
    messages = render_messages(sys_prompt, ctx, trigger, target_uname, profile,
                               length_hint=reply_length_hint(cfg) if cfg else None,
                               focus_text=text,
                               focus_type="speech" if is_anchor_src
                               else "danmaku")

    try:
        draft, latency = llm.chat(messages)
    except Exception as exc:
        # 生成失败同样要还回冷却窗口：这个人 5 秒内的下一条
        # 本该有机会被回复，不能因为这次模型报错就被吞掉。
        if key:
            state.last_trigger_ts.pop(key, None)
        did = store.add_decision(session_id, now_ts, trigger, "", "error",
                                 reason=str(exc)[:200], target_uid=target_uid,
                                 target_uname=target_uname, context=ctx,
                                 model=llm.name)
        store.link_tool_calls(tool_ids, did)
        return {"action": "error", "reason": str(exc)}

    # 工具已经在生成前处理过了，这里拿到的是纯文本回复
    raw = draft or ""
    draft, _, parse_issue = tools_mod.parse_actions(raw)
    draft = draft.strip().splitlines()[0] if draft.strip() else ""
    # 兜底：剥掉开头的 @某某，B 站客户端会自己加，重复会导致 @A@A
    draft = clean.strip_leading_mention(draft)

    ok, reason = guard.evaluate(
        draft, now_ts,
        recent_texts=state.recent_texts,
        duplicate_threshold=(cfg.get("guard") or {}).get(
            "duplicate_threshold", 0.8),
    )
    action = "shadow" if ok else "rejected_by_guard"
    # 被护栏拒掉就回滚冷却：这条没能回复，不该白占这个人的 5 秒窗口，
    # 否则他 5 秒内"本可以回的"下一条会被静默吞掉。
    if not ok and key:
        state.last_trigger_ts.pop(key, None)
    decision_id = store.add_decision(
        session_id, now_ts, trigger, draft, action,
        reason=reason, target_uid=target_uid,
        target_uname=target_uname, context=ctx,
        latency_ms=latency, model=llm.name)
    store.link_tool_calls(tool_ids, decision_id)
    if ok:
        state.accept(now_ts, draft)
    return {"action": action, "draft": draft, "reason": reason,
            "trigger": trigger, "latency_ms": latency,
            "decision_id": decision_id,
            "tools": tool_results,
            "parse_issue": parse_issue}
