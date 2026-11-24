"""回放器：把一场直播的事件按时间轴重放，完整跑一遍决策流程（不发送）。

这是打磨质量的主要手段——同一份历史数据可以用不同 prompt / 模型 / 阈值
反复跑，人工打分对比，而不必拿真实直播间做实验。
"""

from .decide import SendState, decide_one


def load_events(store, session_id, start_s=None, end_s=None, types=None):
    sql = "SELECT * FROM events WHERE session_id=?"
    args = [session_id]
    # start_s=0 时故意不加下界：采集端 start_ts 可能晚于首批事件
    # （实测有 offset_s<0 的弹幕），加下界会把它们静默排除在回放窗外。
    if start_s:
        sql += " AND offset_s>=?"
        args.append(start_s)
    if end_s is not None:
        sql += " AND offset_s<=?"
        args.append(end_s)
    if types:
        sql += " AND type IN (%s)" % ",".join("?" * len(types))
        args += list(types)
    sql += " ORDER BY ts ASC"
    return [dict(r) for r in store.conn.execute(sql, args)]


def replay(store, llm, session_id, start_s=None, end_s=None,
           trigger_types=None, verbose=True, cfg=None, **kw):
    """回放指定时间窗内的事件，返回统计。"""
    if trigger_types is None:
        trigger_types = tuple((cfg or {}).get("triggers", {}).get("types")
                              or ("danmaku",))
    # 必须带上会话的 room_id：工具链写入的记忆是按 (uid, room_id) 分桶的，
    # 不传就会写进 room_id=NULL 的桶里——线上读的是真实 room_id，
    # 结果"回放里记下的别名，上线就不认了"。回归测试里踩到过。
    if not kw.get("room_id"):
        row = store.conn.execute("SELECT room_id FROM sessions WHERE id=?",
                                 (session_id,)).fetchone()
        if row and row[0]:
            kw["room_id"] = int(row[0])
    events = load_events(store, session_id, start_s, end_s, list(trigger_types))
    state = SendState()
    stats = {"scanned": 0, "triggered": 0, "shadow": 0,
             "rejected": 0, "error": 0, "latency_ms": []}
    for e in events:
        stats["scanned"] += 1
        res = decide_one(store, llm, session_id, e, cfg=cfg, state=state, **kw)
        if not res:
            continue
        stats["triggered"] += 1
        act = res.get("action")
        if act == "rejected_by_guard":
            stats["rejected"] += 1
        elif act in stats:
            stats[act] += 1
        if res.get("latency_ms"):
            stats["latency_ms"].append(res["latency_ms"])
        if verbose and act:
            mark = "OK " if act == "shadow" else "-- "
            line = "%s[%s] %s -> %s" % (
                mark, res.get("trigger"), (e.get("uname") or "?"),
                res.get("draft") or "")
            if res.get("reason"):
                line += "   (%s)" % res["reason"]
            print(line)
    lat = stats.pop("latency_ms")
    stats["avg_latency_ms"] = round(sum(lat) / len(lat)) if lat else 0
    return stats
