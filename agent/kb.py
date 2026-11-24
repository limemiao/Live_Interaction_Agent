"""知识库构建：把历史直播内容归纳成"事实卡"和"人物卡"。

分两层（见 docs/agent_reply_optimization.md）：
  事实层：主播的固定信息（称呼、常聊话题、口头禅、房间黑话、雷区）
  人物层：每个观众的印象（常聊话题、说话风格、代表发言）

这两层都是**离线批处理**，一次构建、增量更新，不占实时链路。
"""

import json
import random
import time

# JSON 解析和"不要限制 max_tokens"的说明都在 llm.py（推理模型的坑，
# 两个模块共用同一套约定）
from .llm import extract_json

FACTS_PROMPT = """你在分析一个 B 站直播间的内容，为后续的自动回复机器人建立"背景知识"。

下面是从该直播间抓取的语音转写和弹幕样本。请归纳出这个直播间的基本情况。

要求：
1. 只写样本里能看出来的内容，看不出来的写"未知"，**绝对不要编造**。
2. 输出 JSON，字段如下：
   - streamer_names: 主播的称呼/昵称有哪些（数组）
   - identity: 主播是谁、什么类型的主播（一句话）
   - topics: 常聊的话题（数组，5 个以内）
   - catchphrases: 主播的口头禅（数组，没有就空）
   - slang: 房间里的黑话/梗，以及含义（对象，键是词、值是含义）
   - audience_style: 观众说话的风格特点（一句话）
   - avoid: 从内容判断应该避免的话题（没有就空）
3. 只输出 JSON，不要任何解释。

=== 语音转写样本 ===
{speech}

=== 弹幕样本 ===
{danmaku}
"""

PEOPLE_PROMPT = """你在维护一个 B 站直播间观众的人物档案。

观众昵称：{uname}
粉丝牌：{medal} lv{level}
在本直播间累计发言 {count} 次

【上一次的人物卡】（可能已过时；没有就是"暂无"）
{existing}

【最近的发言】
{messages}

请结合"上一次的人物卡"和"最近的发言"更新这个人的档案。要求：
1. 只依据上面的材料，**不要编造**；信息不足就写"信息较少"。
2. 有旧卡时做**增量更新**：保留旧卡里仍然成立的印象、常聊话题、说话风格和
   代表发言；新发言里补充或修正的部分按新的写。不要因为这次发言少，就把
   旧卡里仍然有效的印象清空。如果旧卡和新发言矛盾（比如以前常聊 A，现在
   只聊 B），以新发言为准。
3. 输出 JSON：
   - summary: 一句话印象（这个人是什么风格、和主播什么关系）
   - topics: 常聊话题（数组，3 个以内）
   - style: 说话风格（一句话，比如"爱开玩笑、常用[dog]表情"）
   - quote: 最能代表他的一条原话。优先从【最近的发言】里选原文；没有合适的
     可以沿用旧卡里的 quote（旧 quote 也是他以前说过的原文），不要改写。
4. 只输出 JSON，不要任何解释。
5. **不要写"在调试机器人 / 自动回复脚本 / 挂机 / 测试机器人"这类内容**，
   也不要提任何和直播间有机器人有关的事——这个直播间对外就是一个普通直播间。
   遇到这类线索，改写成"爱提问、常带动话题、语气玩笑"这种纯风格描述。
"""


def format_person_card(cur):
    """把旧人物卡格式化成提示词里的"上一次的人物卡"。"""
    if not cur:
        return "暂无（这是第一次为他建卡）"
    parts = []
    if cur.get("summary"):
        parts.append("印象：%s" % cur["summary"])
    if cur.get("topics"):
        parts.append("常聊：%s" % cur["topics"])
    if cur.get("style"):
        parts.append("风格：%s" % cur["style"])
    if cur.get("quote"):
        parts.append("代表发言：%s" % cur["quote"])
    if cur.get("updated_at"):
        parts.append("（上次更新：%s）" % cur["updated_at"])
    return "\n".join(parts) or "暂无（这是第一次为他建卡）"


def merge_person_card(old, data):
    """把模型输出合并进旧卡。

    模型这次没给的字段沿用旧值——增量更新时最怕模型只返回半张卡，
    直接覆盖会把以前积累的印象清空。代码层兜底，不依赖模型每次都填全。
    """
    old = old or {}

    def pick(key, limit):
        val = str((data or {}).get(key) or "").strip()
        return (val or str(old.get(key) or "").strip())[:limit]

    topics = (data or {}).get("topics")
    if isinstance(topics, str):
        topics = [t for t in topics.replace("，", "、").split("、")]
    if not isinstance(topics, list):
        topics = []
    topics = [str(t).strip()[:20] for t in topics if str(t).strip()][:5]
    return {
        "summary": pick("summary", 200),
        "topics": ("、".join(topics) or str(old.get("topics") or ""))[:200],
        "style": pick("style", 120),
        "quote": pick("quote", 120),
    }


def build_room_facts(store, llm, room_id, n_speech=150, n_danmaku=150,
                     verbose=True):
    """归纳直播间事实卡。返回解析后的 dict 或 None。"""
    speech = [r["text"] for r in store.conn.execute(
        "SELECT text FROM events WHERE type='speech' AND text IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?", (n_speech,))]
    danmaku = [r["text"] for r in store.conn.execute(
        "SELECT text FROM events WHERE type='danmaku' AND is_bot=0 "
        "AND text IS NOT NULL ORDER BY RANDOM() LIMIT ?", (n_danmaku,))]
    if not speech and not danmaku:
        return None
    prompt = FACTS_PROMPT.format(
        speech="\n".join("- " + s for s in speech),
        danmaku="\n".join("- " + s for s in danmaku))
    if verbose:
        print("  事实卡：样本 语音 %d 条 / 弹幕 %d 条" % (len(speech),
                                                        len(danmaku)))
    # 不传 max_tokens：推理模型的思考也算额度，限制它会让输出被截断/为空
    text, ms = llm.chat([{"role": "user", "content": prompt}],
                        temperature=0.3, timeout=180)
    data = extract_json(text)
    if not data:
        print("  解析失败，原始输出前 200 字：%s" % text[:200])
        return None
    store.set_kb_facts(room_id, json.dumps(data, ensure_ascii=False),
                       evidence="%d 条语音 + %d 条弹幕" % (len(speech),
                                                          len(danmaku)))
    return data


def build_people(store, llm, limit=20, min_msg=5, verbose=True,
                 stale_hours=None, max_people=None):
    """为活跃观众归纳人物卡。返回成功条数。

    stale_hours：跳过 N 小时内刚归纳过的卡（None = 不跳过）。
    max_people：单次最多处理几个人；None 或 0 = 不限。

    每张卡都会带上旧卡内容做增量更新（PEOPLE_PROMPT + merge_person_card），
    所以重复跑不会把之前积累的印象刷掉；模型漏填的字段也会沿用旧值。
    """
    ok = 0
    skipped = 0
    for u in store.top_speakers(limit=limit, min_msg=min_msg):
        if max_people and ok >= max_people:
            break
        cur = store.kb_person(u["uid"])
        if stale_hours is not None and cur and cur.get("updated_at"):
            try:
                made = time.mktime(time.strptime(cur["updated_at"],
                                                 "%Y-%m-%d %H:%M:%S"))
                if (time.time() - made) / 3600.0 < stale_hours:
                    skipped += 1
                    continue
            except ValueError:
                pass
        msgs = store.user_messages(u["uid"], limit=40)
        if len(msgs) < 3:
            continue
        sample = "\n".join("- " + (m["text"] or "") for m in reversed(msgs))
        prompt = PEOPLE_PROMPT.format(
            uname=u["uname"] or "?", medal=u["medal_name"] or "无",
            level=u["medal_level"] or 0, count=u["msg_count"],
            existing=format_person_card(cur),
            messages=sample)
        try:
            text, _ = llm.chat([{"role": "user", "content": prompt}],
                               temperature=0.3, timeout=180)
        except Exception as exc:
            if verbose:
                print("  %s 归纳失败: %s" % (u["uname"], exc))
            continue
        data = extract_json(text)
        if not data:
            continue
        card = merge_person_card(cur, data)
        store.set_kb_person(
            u["uid"], u["uname"], card["summary"],
            topics=card["topics"], style=card["style"],
            quote=card["quote"], msg_count=u["msg_count"])
        ok += 1
        if verbose:
            print("  %-20s %s" % (u["uname"], card["summary"][:40]))
    if verbose and skipped:
        print("  跳过 %d 张（%s 小时内刚更新过）" % (skipped, stale_hours))
    return ok


ROOM_SUMMARY_PROMPT = """下面是一个直播间最近一段时间的弹幕和主播发言。
请用 2~3 句话概括"这段时间在聊什么、发生了什么"，只写看得出来的事实，不要编造。

=== 主播发言 ===
{speech}

=== 弹幕 ===
{danmaku}

直接输出概括文字，不要 JSON，不要解释。
"""


MERGE_PROMPT = """下面是同一场直播的「旧摘要」和「新进展」。
请把它们合并压缩成一段不超过 {budget} 字的摘要，按时间顺序保留：
话题是怎么变的、发生过什么值得记住的事。只写已有的内容，不要编造，
不要标题，不要分点。

【旧摘要】
{old}

【新进展】
{new}
"""

SUMMARY_KEEP_CHARS = 2000


def _compress_summary(llm, old, new, budget=SUMMARY_KEEP_CHARS):
    """把"旧摘要 + 新进展"再压一次（摘要的摘要）。失败就退化成保留最新。"""
    try:
        text, _ = llm.chat(
            [{"role": "user", "content": MERGE_PROMPT.format(
                budget=budget, old=old, new=new)}],
            temperature=0.2, timeout=180)
        text = (text or "").strip()
        if text:
            return text[:budget]
    except Exception:
        pass
    return (old + "\n" + new)[-budget:]


def build_session_summary(store, llm, session_id, through_ts, window_s=600,
                          keep_chars=SUMMARY_KEEP_CHARS, min_events=5,
                          verbose=False):
    """滚动摘要：把窗口内的**新增**内容压成几句话，累积成本场摘要。

    原实现有两个毛病：
      1）每次都取"最近 10 分钟"，与上次摘要重叠，同一段被反复概括；
      2）累积后直接 `content[-2000:]` 截断 —— 开场内容会被静默丢掉，
         长直播下来只记得最后一段。
    改成：只取上次 `through_ts` 之后的新事件；超长时让模型把
    "旧摘要 + 新进展"再压一次，而不是砍尾巴。
    """
    prev = store.session_summary(session_id) or {}
    prev_content = (prev.get("content") or "").strip()
    # 有上次摘要就从它之后接着做；没有就取最近 window_s 这一段
    since = prev.get("through_ts") or (through_ts - window_s * 1000)
    rows = store.conn.execute(
        "SELECT type, uname, text FROM events WHERE session_id=? AND ts>? "
        "AND ts<=? AND text IS NOT NULL AND is_noise=0 ORDER BY ts LIMIT 300",
        (session_id, since, through_ts)).fetchall()
    if len(rows) < min_events:
        return None
    speech = [r[2] for r in rows if r[0] == "speech"][:80]
    danmaku = [r[2] for r in rows if r[0] == "danmaku"][:80]
    prompt = ROOM_SUMMARY_PROMPT.format(
        speech="\n".join("- " + s for s in speech) or "（无）",
        danmaku="\n".join("- " + s for s in danmaku) or "（无）")
    text, _ = llm.chat([{"role": "user", "content": prompt}],
                       temperature=0.3, timeout=180)
    text = (text or "").strip()
    if not text:
        return None
    content = (prev_content + "\n" + text) if prev_content else text
    if len(content) > keep_chars:
        content = _compress_summary(llm, prev_content, text, keep_chars)
    store.set_session_summary(session_id, content, through_ts)
    if verbose:
        print("  摘要更新：新增 %d 条事件 → 摘要 %d 字"
              % (len(rows), len(content)))
    return text


def maybe_update_summary(store, llm, session_id, now_ts=None, min_events=40,
                         window_s=600, keep_chars=SUMMARY_KEEP_CHARS,
                         verbose=False):
    """本场自上次摘要以来攒够新内容才跑，省掉没必要的模型调用。

    实时服务每隔几分钟调一次它：新增不足 min_events 条就直接返回，
    一个安静直播间不会产生任何费用。
    """
    now_ts = int(now_ts or time.time() * 1000)
    prev = store.session_summary(session_id) or {}
    if store.count_events_since(session_id, prev.get("through_ts") or 0) \
            < min_events:
        return None
    return build_session_summary(store, llm, session_id, now_ts,
                                 window_s=window_s, keep_chars=keep_chars,
                                 min_events=min_events, verbose=verbose)
