"""直播间情报的批量抽取（独立模块，与聊天机器人完全分开）。

为什么必须单独一个模块、单独的提示词：

  聊天机器人（小助手）的提示词是「人设 + 底线 + 上下文 + 长度约束」，
  目标是**说一句像真人的弹幕**，要口语化、要有人味、长度受限。

  这里是后台**情报整理**：目标是把主播说过的话整理成结构化、可复用的事实，
  要求恰好相反——严格、保守、宁缺勿滥，还要标注原文依据。两套要求互相冲突，
  混在一个提示词里两边都会变差（比如聊天提示词里的"不要编造、说得像人话"
  会让抽取变得爱发挥；抽取提示词里的"输出 JSON"会污染聊天回复）。

所以：本模块自带提示词、自带输出协议，只依赖 `store` 和 `llm`，
不引用 personas / decide / 聊天相关的任何东西。

触发方式：实时服务按 `context.summary_interval_s` 定时调用
`maybe_extract_room_facts()`；它自己判断素材够不够、按事件 id 增量处理。
"""

import time

from .lexicon import CORRECTIONS
from .llm import extract_json
from .guard import looks_like_self_identity

SOURCE_TAG = "批量抽取"

# 已知的语音误识别（来自 agent/lexicon.py，人工整理的真实语料）。
# 给模型看这张表，它才知道"肩章"其实是舰长、"虚虚念"其实是示例主播——
# 否则这些错字会被当成直播间事实原样记下来。
_MISHEAR = "、".join("%s→%s" % (k, v) for k, v in list(CORRECTIONS.items())[:40])

EXTRACT_PROMPT = """你是后台的情报整理员，不是直播间的聊天机器人——不要寒暄、
不要解释，只做整理。下面是一段直播间的内容，请整理成结构化情报。

【素材说明（很重要）】
· 「主播：」开头的行是**本地语音识别的转写**，有同音错字，例如
  「舰长」会被写成「肩章」，「示例主播」会被写成「虚虚念」，
  也可能出现重复词（"下午下午三点"）或半截句子。
· 「观众：」开头的行是弹幕原文，一般没有错字，但它只反映话题，
  **不能当作事实依据**。
· 这段时间是 {day} 的 {span}。
· 已知的误识别对照（可以据此把词改回规范写法）：{mishear}

【输出 JSON】
{{
  "topic": "这段时间在聊什么，不超过 12 字；看不出明显话题就给空字符串",
  "facts": [
    {{"key": "短名称（不超过 8 字）",
      "value": "内容（不超过 40 字）",
      "quote": "依据的主播原话片段"}}
  ],
  "slang": [{{"term": "黑话", "meaning": "含义",
             "quote": "依据的主播原话片段"}}]
}}

【判定规则】
1. facts 只记**主播亲口说的、以后还有用**的信息：
   直播作息（几点开播/播到几点）、直播安排（哪天播哪天不播、什么时候休息）、
   计划（要做什么节目/联动/活动）、直播间规则（怎么领牌、什么不能刷）。
2. 以下**一律不要记**：一次性的闲聊、情绪和感叹、观众说的话、
   对别人的评价、天气和饭菜这类无关内容、你自己推测出来的东西。
3. 明显不通顺或前后矛盾的句子（多半是识别错了）**不要记为事实**；
   但如果能从上下文判断出它其实在说什么，可以按规范写法记录，
   并在 quote 里放原话片段。
4. 涉及具体日期的安排，value 里带上日期，例如「10-17 不播」——
   以后被引用时不会因为"明天"过时而失效。
5. key 尽量沿用【已有事实】里的同名 key（同名表示更新，不是新增）；
   确实没有合适的已有 key 才新建。
6. facts 最多 {max_facts} 条，没有就给空数组。value 里不要写"主播说"这类话。
7. slang 只记**确实反复出现、且能从上下文看出含义**的房间黑话；
   只出现一次或含义靠猜的一律不写。
8. **绝对不要记录"小助手（机器人账号）是 AI / 程序 / 机器人 / 助手 / 模型"这类说法**，
   也不要记录它"有什么模式可切换"。房间里的人这么开玩笑也不要记——
   这个账号对外就是一个普通观众，写进去会和它的身份冲突。
9. 只输出 JSON，不要 markdown 代码块，不要任何解释。

【已有事实】（避免重复；沿用同名 key 即为更新）
{existing}

【待整理内容】
{content}
"""


def _existing_notes(store, room_id, limit=20):
    """把已有的房间事实/黑话列给模型，让它复用 key、避免重复新增。"""
    lines = []
    rows = store.conn.execute(
        "SELECT key, value FROM memories WHERE room_id=? AND uid IS NULL "
        "AND (key LIKE 'fact:%' OR key LIKE 'slang:%') "
        "ORDER BY priority DESC, id DESC LIMIT ?", (room_id, limit)).fetchall()
    for k, v in rows:
        lines.append("- %s = %s" % (k, v))
    return "\n".join(lines) or "（还没有）"


def _write_room_memory(store, room_id, key, value, source):
    """写房间级记忆；内容没变就跳过，避免刷新时间、打乱"新的在前"顺序。"""
    if store.get_memory(None, room_id, key) == value:
        return False
    store.set_memory(None, room_id, key, value, 3, source, None)
    return True


def extract_room_facts(store, llm, session_id, room_id, since_id=0,
                       limit=120, max_facts=3, daily_limit=40,
                       include_danmaku=True, timeout=120, verbose=False):
    """抽取一批（`since_id` 之后的前 `limit` 条）并入库。

    返回 (写入条数, 最后处理的事件 id)。游标用事件 id 而不是时间戳，
    所以不会漏也不会重；素材不够或解析失败时返回 (0, 原游标或已处理的 id)。
    """
    if not room_id:
        return 0, since_id
    kinds = "type='speech'"
    if include_danmaku:
        kinds += " OR (type='danmaku' AND is_bot=0)"
    rows = store.conn.execute(
        "SELECT id, type, ts, uname, text FROM events "
        "WHERE session_id=? AND id>? AND is_noise=0 AND IFNULL(text,'')<>'' "
        "AND (%s) ORDER BY id LIMIT ?" % kinds,
        (session_id, since_id, limit)).fetchall()
    if not rows:
        return 0, since_id
    last_id = rows[-1][0]
    speech = [r for r in rows if r[1] == "speech"]
    # 主播还没怎么说话就别急着花调用（弹幕再多也抽不出事实）
    if len(speech) < 3:
        return 0, since_id

    body_lines = []
    for _id, typ, _ts, uname, text in rows:
        body_lines.append("%s：%s" % ("主播" if typ == "speech"
                                     else (uname or "观众"), text))
    # 单批别太大：输入越长，推理模型思考越久，越容易读超时；
    # 而且事实本来就是稀疏的，一段 1~2 分钟的话足够抽出该抽的东西。
    body = "\n".join(body_lines)[-3500:]
    ts_from = time.localtime(rows[0][2] / 1000)
    ts_to = time.localtime(rows[-1][2] / 1000)
    span = "%s~%s" % (time.strftime("%H:%M", ts_from),
                      time.strftime("%H:%M", ts_to))

    prompt = EXTRACT_PROMPT.format(
        day=time.strftime("%Y-%m-%d", ts_to),
        span=span,
        mishear=_MISHEAR,
        max_facts=max_facts,
        existing=_existing_notes(store, room_id),
        content=body)
    # 两点讲究：
    # 1）**不要传 max_tokens**——deepseek-flash 是推理模型，思考 token 也算
    #    在那个额度里，给小了会把额度全花在思考上、正文返回空（踩过三次）。
    # 2）给足超时并重试一次：这类批量调用输入长、思考久，默认 60 秒会读超时。
    text = ""
    last_exc = None
    for attempt in (1, 2):
        try:
            text, _ = llm.chat([{"role": "user", "content": prompt}],
                               temperature=0.2, timeout=timeout)
            if (text or "").strip():
                break
            last_exc = "模型返回空（多半是思考吃掉了输出额度）"
        except Exception as exc:
            last_exc = "%s: %s" % (type(exc).__name__, exc)
            if attempt == 1:
                time.sleep(2)
    if not (text or "").strip():
        if verbose:
            print("  抽取失败（已重试）：%s" % last_exc)
        return 0, last_id
    data = extract_json(text)
    if not data:
        if verbose:
            print("  抽取结果解析失败：%s" % text[:120])
        return 0, last_id

    today = time.strftime("%Y-%m-%d")
    used = store.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE room_id=? AND source=? "
        "AND uid IS NULL AND created_at LIKE ?",
        (room_id, SOURCE_TAG, today + "%")).fetchone()[0]
    written = 0

    topic = str(data.get("topic") or "").strip()[:12]
    if topic and store.session_topic(session_id, room_id) != topic:
        store.set_session_topic(session_id, topic, room_id,
                                source="抽自 %s" % span)
        written += 1

    for item in (data.get("facts") or [])[:max_facts]:
        if used + written >= daily_limit or not isinstance(item, dict):
            break
        key = str(item.get("key") or "").strip()[:8]
        val = str(item.get("value") or "").strip()[:40]
        if not key or not val:
            continue
        # 代码级硬拦：不许把"小助手是 AI/程序/助手"写成房间事实
        if looks_like_self_identity(key + " " + val):
            if verbose:
                print("  (跳过身份冲突的事实) %s = %s" % (key, val))
            continue
        # 原文依据存进 source 列，出问题能追到"是哪句话写进来的"
        quote = str(item.get("quote") or "").strip()[:60] or SOURCE_TAG
        if _write_room_memory(store, room_id, "fact:" + key, val, quote):
            written += 1

    for item in (data.get("slang") or [])[:2]:
        if used + written >= daily_limit or not isinstance(item, dict):
            break
        term = str(item.get("term") or "").strip()[:10]
        mean = str(item.get("meaning") or "").strip()[:40]
        if not term or not mean:
            continue
        if looks_like_self_identity(term + " " + mean):
            if verbose:
                print("  (跳过身份冲突的黑话) %s = %s" % (term, mean))
            continue
        # 和 fact 一样留原文依据（三个区段的溯源粒度保持一致）
        quote = str(item.get("quote") or "").strip()[:60] or SOURCE_TAG
        if _write_room_memory(store, room_id, "slang:" + term, mean, quote):
            written += 1

    if verbose:
        print("  抽取：%d 条素材（到事件 #%d）→ 写入 %d 条"
              % (len(rows), last_id, written))
    return written, last_id


def maybe_extract_room_facts(store, llm, session_id, room_id, min_lines=20,
                             limit=120, max_facts=3, daily_limit=40,
                             timeout=120, verbose=False):
    """实时服务定时调它：攒够新素材才抽一次。

    游标存 settings（按场次），所以服务重启不会重抽、也不会漏。
    """
    if not room_id:
        return 0
    skey = "kb_extract:" + session_id
    since_id = int(store.get_setting(skey, 0) or 0)
    pending = store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE session_id=? AND id>? "
        "AND is_noise=0 AND type='speech'", (session_id, since_id)
    ).fetchone()[0]
    if pending < min_lines:
        return 0
    written, last_id = extract_room_facts(
        store, llm, session_id, room_id, since_id=since_id, limit=limit,
        max_facts=max_facts, daily_limit=daily_limit, timeout=timeout,
        verbose=verbose)
    if last_id > since_id:
        store.set_setting(skey, str(last_id))
    return written
