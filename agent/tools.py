"""工具链：解析模型输出的 actions 并执行，带权限校验与审计。

分档（见 docs/agent_persona_and_tools.md）：
  A 档 观众对自己：set_alias / set_self_note / set_preference / forget_self
  B 档 情境受限：set_topic / add_slang / add_room_fact（仅主播语音）
  C 档 仅操作者：bot_ignore / bot_ignore_cancel / switch_persona / purge_history

**权限是代码级硬校验，不靠提示词。**
"""

import json
import time

from . import guard

SELF_TOOLS = {"set_alias", "set_self_note", "set_preference", "forget_self"}
CTX_TOOLS = {"set_topic", "add_slang"}
ROOM_FACT_TOOLS = {"add_room_fact"}
OPERATOR_TOOLS = {"bot_ignore", "bot_ignore_cancel", "switch_persona",
                  "purge_history"}
ALL_TOOLS = SELF_TOOLS | CTX_TOOLS | ROOM_FACT_TOOLS | OPERATOR_TOOLS

MAX_NOTE_LEN = 60
DAILY_WRITE_LIMIT = 8        # 每人每天最多写入次数
MAX_ACTIONS = 3              # 一条消息最多触发几个工具
# 房间级写入（主播语音说的 fact/slang/topic）单独限额：它们的 source_uid 是
# NULL，用"每人每天"的计数方式永远为 0，等于没有上限——主播一晚上叫几十次
# 名字，记忆表就会被灌满。这里按房间计。
ROOM_WRITE_TOOLS = CTX_TOOLS | ROOM_FACT_TOOLS
ROOM_WRITE_LIMIT = 12        # 每个房间每天最多写入次数

PRIORITY = {"model": 1, "self": 2, "anchor": 3, "operator": 9}

# ---- 第一步：先判断要不要调工具（独立的小任务，比"边说话边写JSON"可靠）----

JUDGE_PROMPT = """你叫「小助手」，是直播间里的一个互动账号。现在有人对着你说话，
你要判断**这条话是不是在让你做事**，是的话该用哪个工具。

重要：说话人可能没有直接 @ 你，但话语里出现了你的名字，或者明显是在
对你下指令（"以后叫我X"），那就当作是在对你说。

关于你的身份，要看清楚两层区别：
- **对外的表现**：你在直播间里表现得像个普通观众，这是给其他人看的。
- **对内的事实**：当说话人是【操作者】时，他是在以管理身份给你下指令，
  你有能力执行，**必须照做**，不要回答"我只是个观众/我没这权限"。
  操作者的指令不需要向任何人解释来源。

【可以调用的工具】
{tools}
{source_note}
【当前情况】
说话人：{uname}（uid={uid}）
这个人是不是操作者：{is_operator}
他说的话（这就是你要判断的那条）：{text}
最近几条弹幕：
{recent}

判断规则：
- "以后叫我X" / "叫我X就行"  → set_alias，name 填 X
- "记住我/我是……" 这类关于他本人的事 → set_self_note（note 填内容本身）
- "忘掉刚才说的" / "别记了XXX" → forget_self，key 填**要忘掉的那件事的关键词**
  （例如"忘掉我在考研的事" → key="考研"，不用管内部怎么存的）
- 操作者说"换成XX人设" / "换个风格" → switch_persona
- 操作者说"把某人拉黑/别理某人" → bot_ignore
- 其他情况**一律不需要工具**。闲聊、提问、开玩笑、感叹都不需要。

严格要求：
1. 只有他的话**明确表达了上面某个意图**才调用，拿不准就不要调。
2. **一句话里如果有多个意图，全部列出来**（最多 3 条），不要只做第一个。
   例："叫我大王，顺便记住我是考研的" → set_alias + set_self_note 两条。
3. 没有合适工具时，宁可返回空，也不要硬凑。闲聊、提问、感叹返回空。
4. 只输出 JSON，不要任何解释。两种格式都接受：
   {{"actions": [{{"tool": "工具名", "args": {{...}}}},
                {{"tool": "工具名", "args": {{...}}}}]}}
   {{"tool": "工具名", "args": {{...}}}}          （只有一个意图时）
   没有任何意图时输出：{{"actions": []}}
"""

# 廉价的意图预筛：命中这些词、或说话人是操作者，才值得花一次模型调用去判断
INTENT_HINTS = ["叫我", "记住", "记一下", "忘掉", "别记", "忘了", "换成",
                "切换", "拉黑", "禁言", "忽略", "人设", "以后叫", "喊我"]


def should_judge(text, is_operator=False):
    """要不要为这条消息跑一次工具判定。

    以前对操作者是"一律判定"，结果他随口聊一句也会被当成指令去判定、
    然后机器人回一句（实测被自己刷屏）。现在指令词对所有人都一样要求，
    操作者放宽的地方只体现在触发层：他**不需要叫名字**也能算"在跟我说话"。
    """
    t = text or ""
    return any(h in t for h in INTENT_HINTS)


def available_tools(is_operator, is_anchor=False):
    """按身份列出可用工具，用于提示词（**不是权限校验**——校验在 execute 里）。

    is_anchor=True 时说话人是主播（语音转写）：他没有"自己"可写，
    只列记录类工具（B 档），A 档"观众对自己"的工具对他不适用。
    """
    if is_anchor:
        base = [
            "add_room_fact(key, value) —— 记录主播亲口说的直播间事实"
            "（作息、安排、计划、规则）",
            "set_topic(topic) —— 记录本场当前话题",
            "add_slang(term, meaning) —— 记录房间黑话及含义",
        ]
    else:
        base = [
            "set_alias(uid, name) —— 记住「以后叫我 X」",
            "set_self_note(uid, note) —— 记住关于他本人的事",
            "set_preference(uid, key, value) —— 记住他的偏好",
            "forget_self(uid, key) —— 忘掉之前记的",
        ]
    if is_operator:
        base += [
            "switch_persona(persona) —— 切换人设。可选值："
            "normal(普通观众) / assistant(专业助手) / humorous(幽默风趣) / "
            "cute(可爱粘人) / tsundere(傲娇高冷)",
            "bot_ignore(uid, uname) —— 让机器人不再回应某个人",
            "bot_ignore_cancel(uid) —— 解除忽略",
            "purge_history(session_id 或 uid) —— 删除历史记录",
        ]
    return "\n".join("  - " + b for b in base)


def judge(llm, text, uid, uname, recent_lines, is_operator, is_anchor=False):
    """让模型判断该调哪些工具。返回 [(tool, args), ...]，空列表表示不需要。

    支持一次多条：像"叫我大王，顺便记住我是考研的"这种话包含两个意图，
    返回单条会静默丢掉一个。成本几乎为零（同一个响应里多几个字段）。
    上限 MAX_ACTIONS 条：模型偶尔会把一句闲聊拆成四五个"意图"，
    执行侧再多也没意义，还会白写一堆记忆。

    is_anchor=True 表示这句话来自主播的语音转写：他是在陈述，
    不是在下指令，提示词和工具清单都要换一套。
    """
    source_note = ""
    if is_anchor:
        source_note = (
            "注意：这条话来自【主播的语音转写】（可能有同音错别字）。"
            "主播是在**陈述**，不是在对机器人下指令：\n"
            "  - 主播陈述了直播间的事实（作息、计划、安排、规则）"
            " → add_room_fact\n"
            "  - 主播提到当前在聊的话题 → set_topic\n"
            "  - 主播反复使用某个黑话 → add_slang\n"
            "  - set_alias / set_self_note 这类「观众对自己」的工具"
            "对主播不适用，不要用。\n"
            "  - 主播的话是语音转写，明显不通顺的句子不要当成事实记录。\n")
    prompt = JUDGE_PROMPT.format(
        tools=available_tools(is_operator, is_anchor=is_anchor),
        source_note=source_note,
        uname=uname or "主播",
        uid=uid or 0, is_operator="是" if is_operator else "不是",
        text=text,
        recent="\n".join("  " + l for l in (recent_lines or [])[-4:]) or "  （无）")
    try:
        raw, _ = llm.chat([{"role": "user", "content": prompt}],
                          temperature=0.1)
    except Exception:
        return []
    t = (raw or "").strip()
    if "```" in t:
        for part in t.split("```"):
            if part.strip().startswith("{"):
                t = part.strip()
                break
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return []
    try:
        data = json.loads(t[i:j + 1])
    except Exception:
        return []

    # 兼容两种写法：{"tool":..,"args":..} 和 {"actions":[{...},{...}]}
    items = data.get("actions")
    if not isinstance(items, list):
        items = [data] if data.get("tool") else []
    out = []
    for it in items:
        if len(out) >= MAX_ACTIONS:
            break
        if not isinstance(it, dict):
            continue
        tool = it.get("tool")
        if not tool or tool not in ALL_TOOLS:
            continue
        args = it.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        args.setdefault("uid", uid)
        out.append((tool, args))
    return out


def parse_actions(raw):
    """从模型输出里解析 {reply, actions}。返回 (reply, actions, 问题原因)。

    解析失败时把原文当回复——宁可丢记忆，不能丢回复。
    """
    if not raw:
        return "", [], "empty"
    t = raw.strip()
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            if p.startswith("{"):
                t = p
                break
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            data = json.loads(t[i:j + 1])
            reply = str(data.get("reply") or "").strip()
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                actions = []
            actions = [a for a in actions if isinstance(a, dict)
                       and a.get("tool") in ALL_TOOLS]
            return reply, actions, None
        except Exception:
            pass
    return raw.strip(), [], "not_json"


def _today():
    return time.strftime("%Y-%m-%d")


def _over_quota(store, tool, uid, room_id):
    """今天的写入量是否已经到顶。

    观众写入（A 档）按人算，房间级写入（B 档，source_uid 为 NULL）按房间算——
    后者用"每人"的口径永远数不到，等于没有上限。
    """
    if tool in ROOM_WRITE_TOOLS:
        return store.writes_today(uid, room_id) >= ROOM_WRITE_LIMIT
    return store.writes_today(uid) >= DAILY_WRITE_LIMIT


def execute(store, actions, ctx):
    """执行一批 action。ctx 需要: uid, uname, operator_uid, room_id,
    session_id, source_text, source_is_anchor。

    返回执行结果列表；每条都写进 tool_calls 审计表。
    """
    results = []
    uid = ctx.get("uid")
    is_operator = bool(uid) and uid == ctx.get("operator_uid")
    for act in actions or []:
        tool = act.get("tool")
        status, reason = "ok", ""
        try:
            if tool not in ALL_TOOLS:
                status, reason = "unknown_tool", "未知工具"
            elif tool in OPERATOR_TOOLS and not is_operator:
                status, reason = "rejected_by_policy", "只有操作者可以触发"
            elif tool in (ROOM_FACT_TOOLS | CTX_TOOLS) \
                    and not ctx.get("source_is_anchor"):
                # 房间级记录（事实/黑话/话题）只能由主播的话写入：
                # 它们会被注入所有人的提示词，观众能写就等于能污染公共上下文。
                # 原来只校验了 add_room_fact，add_slang / set_topic 漏了。
                status, reason = "rejected_by_policy", "只接受主播自己说的话"
            elif tool in SELF_TOOLS and not uid:
                # A 档是"观众对自己"——语音事件的触发者 uid 为 NULL,
                # 没有"自己"可写。放行会把主播的话写成 uid=NULL 的记忆。
                status, reason = "rejected_by_policy", "没有可写入的用户"
            elif tool in SELF_TOOLS and int(act.get("uid") or 0) not in (0, uid or 0):
                status, reason = "rejected_by_policy", "只能改自己的记录"
            elif _over_quota(store, tool, uid, ctx.get("room_id")) and tool not in OPERATOR_TOOLS:
                status, reason = "rejected_by_policy", "今日写入次数已达上限"
            else:
                status, reason = _run(store, tool, act, ctx)
        except Exception as exc:
            status, reason = "error", str(exc)[:120]
        call_id = store.add_tool_call(ctx.get("decision_id"), tool,
                                      json.dumps(act, ensure_ascii=False),
                                      status, reason)
        results.append({"tool": tool, "status": status, "reason": reason,
                        "id": call_id})
    return results


def _run(store, tool, act, ctx):
    uid = ctx.get("uid")
    room = ctx.get("room_id")
    src = ctx.get("source_text") or ""
    if tool == "set_alias":
        name = str(act.get("name") or "").strip()[:20]
        if not name:
            return "rejected_by_policy", "别名为空"
        store.set_memory(uid, room, "alias", name, PRIORITY["self"],
                         src, uid)
        return "ok", ""
    if tool == "set_self_note":
        note = str(act.get("note") or "").strip()[:MAX_NOTE_LEN]
        if not note:
            return "rejected_by_policy", "内容为空"
        # 键用内容指纹：同样的内容重复说是更新，不同内容是新增。
        # 早前用"内容前 12 字"当键，两条不同备注会互相覆盖。
        store.set_note(uid, room, note, PRIORITY["self"], src, uid)
        return "ok", ""
    if tool == "set_preference":
        val = str(act.get("value") or "").strip()[:MAX_NOTE_LEN]
        store.set_memory(uid, room, "pref:" + str(act.get("key") or "?")[:12],
                         val, PRIORITY["self"], src, uid)
        return "ok", ""
    if tool == "forget_self":
        # key 为空会被拼成 LIKE '%'，把这个人的别名、备注、偏好一次全删掉。
        # 模型偶尔就是会漏参数，这种"静默清空"必须挡在执行之前。
        key = str(act.get("key") or "").strip()
        if not key:
            return "rejected_by_policy", "没说要忘掉哪一条"
        # 按"人话"匹配：模型说"忘掉考研的事"，按内容删，不必猜内部键
        n = store.forget_matching(uid, room, key)
        if not n:
            return "ok", "没有找到关于「%s」的记录" % key
        return "ok", "已删掉 %d 条关于「%s」的记录" % (n, key)
    if tool == "set_topic":
        store.set_session_topic(ctx.get("session_id"),
                                str(act.get("topic") or "")[:40], room,
                                source=src[:60] or "主播语音")
        return "ok", ""
    if tool == "add_slang":
        term = str(act.get("term") or "").strip()[:20]
        meaning = str(act.get("meaning") or "").strip()[:MAX_NOTE_LEN]
        if not term or not meaning:
            return "rejected_by_policy", "参数不全"
        # 房间级记录会被注入所有人的提示词：不许写"小助手是 AI/程序"这类内容
        if guard.looks_like_self_identity(term + " " + meaning):
            return "rejected_by_policy", "这类内容不能写进房间事实"
        store.set_memory(None, room, "slang:" + term, meaning,
                         PRIORITY["anchor"], src, uid)
        return "ok", ""
    if tool == "add_room_fact":
        key = str(act.get("key") or "").strip()[:20]
        val = str(act.get("value") or "").strip()[:MAX_NOTE_LEN]
        if not key or not val:
            return "rejected_by_policy", "参数不全"
        if guard.looks_like_self_identity(key + " " + val):
            return "rejected_by_policy", "这类内容不能写进房间事实"
        store.set_memory(None, room, "fact:" + key, val,
                         PRIORITY["anchor"], src, uid)
        return "ok", ""
    if tool == "bot_ignore":
        fu = int(act.get("uid") or 0)
        if not fu:
            return "rejected_by_policy", "缺少 uid"
        store.set_memory(fu, room, "ignore", str(act.get("uname") or ""),
                         PRIORITY["operator"], src, uid)
        return "ok", ""
    if tool == "bot_ignore_cancel":
        store.delete_memory(int(act.get("uid") or 0), room, "ignore")
        return "ok", ""
    if tool == "switch_persona":
        from .personas import PERSONAS
        key = str(act.get("persona") or "")
        if key not in PERSONAS:
            return "rejected_by_policy", "没有这个人设：" + key
        store.set_setting("persona", key)
        return "ok", ""
    if tool == "purge_history":
        n = store.purge(session_id=act.get("session_id"),
                        uid=act.get("uid") and int(act["uid"]))
        return "ok", "删除 %d 条" % n
    return "unknown_tool", "未实现"
