"""命令行入口。

用法示例:
  python -m agent.cli ingest --danmaku danmaku_x.txt --transcript transcript_x.txt
  python -m agent.cli stats
  python -m agent.cli users --limit 10
  python -m agent.cli search --query 水手服
  python -m agent.cli replay --session <id> --offline --bot-name 小助手
  python -m agent.cli decisions --session <id>

注意: 本版本只做只读的影子模式，**不发送任何弹幕**。
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from .decide import DEFAULT_KEYWORDS, DEFAULT_PERSONA
from .config import load_config
from .ingest import ingest
from .llm import LLM, NullLLM
from .replay import replay
from .store import Store

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE.parent / "data" / "events.db"


def _store(args):
    return Store(args.db)


def cmd_ingest(args):
    store = _store(args)
    date = dt.date.fromisoformat(args.date) if args.date else None
    sid, info = ingest(store, args.danmaku, args.transcript, args.room,
                       date, args.session, cfg=args.cfg)
    print("session_id: %s" % sid)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args):
    store = _store(args)
    sessions = store.sessions()
    if not sessions:
        print("情报库还是空的，先跑 ingest。")
        return 0
    for s in sessions:
        st = store.stats(s["id"])
        print("[%s] room=%s 弹幕=%d 语音=%d 独立用户=%d 时长=%.0fs" % (
            s["id"], s["room_id"], st["danmaku"], st["speech"],
            st["users"], st["span_s"]))
    return 0


def cmd_users(args):
    store = _store(args)
    for u in store.top_users(args.limit):
        print("%-22s uid=%-12s 发言=%-4d 粉丝牌=%s lv%d" % (
            (u["uname"] or "?")[:20], u["uid"], u["msg_count"],
            u["medal_name"] or "无", u["medal_level"] or 0))
    return 0


def cmd_search(args):
    store = _store(args)
    for e in store.search_text(args.query, limit=args.limit,
                               session_id=args.session):
        when = dt.datetime.fromtimestamp(e["ts"] / 1000).strftime("%H:%M:%S")
        print("[%s] %s: %s" % (when, e["uname"] or "?", e["text"]))
    return 0


def cmd_replay(args):
    store = _store(args)
    sessions = store.sessions()
    sid = args.session or (sessions[0]["id"] if sessions else None)
    if not sid:
        print("没有可回放的场次。")
        return 1
    # 语义检索：没有它，回放出来的回复就跟线上不一样（线上是带历史检索的），
    # 调参就失去意义了。
    from .embed import open_index
    index = open_index(store, log=lambda m: print("[回放] " + m),
                       enabled=not args.no_index)
    api_key = args.api_key or args.cfg["llm"].get("api_key")
    if args.offline or not api_key:
        llm = NullLLM()
        if not args.offline:
            print("提示: 配置里没有 api_key，自动切换到离线占位模型。")
    else:
        llm = LLM(args.cfg, api_key=api_key, base_url=args.base_url,
                  model=args.model)
    if args.keywords:
        args.cfg["triggers"]["keywords"] = args.keywords.split(",")
    if args.bot_name:
        args.cfg["reply"]["bot_name"] = args.bot_name
    if args.persona_file:
        args.cfg["reply"]["persona"] = Path(args.persona_file).read_text(
            encoding="utf-8").strip()
    print("回放场次 %s | 模型 %s | 触发词 %s" % (
        sid, llm.name, args.cfg["triggers"].get("keywords")))
    print("-" * 60)
    stats = replay(store, llm, sid, args.start, args.end,
                   cfg=args.cfg, min_interval_s=args.min_interval,
                   index=index)
    print("-" * 60)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_decisions(args):
    store = _store(args)
    rows = store.decisions(args.session, args.limit)
    if not rows:
        print("还没有决策记录。")
        return 0
    for d in rows:
        when = dt.datetime.fromtimestamp(d["ts"] / 1000).strftime("%H:%M:%S")
        print("[%s] %-8s %-16s %s" % (when, d["action"], d["trigger"],
                                      d["draft"] or ""))
        if d["reason"]:
            print("           原因: %s" % d["reason"])
    return 0


def cmd_send(args):
    """把某条决策（或指定文本）真正发到直播间。必须显式指定。"""
    from .execute import load_cookies, send_danmaku

    text = args.text
    reply_mid = args.reply_mid
    if args.decision:
        store = _store(args)
        rows = [d for d in store.decisions(None, 500) if d["id"] == args.decision]
        if not rows:
            print("找不到 decision id=%s" % args.decision)
            return 1
        d = rows[0]
        text = text or d["draft"]
        reply_mid = reply_mid or d["target_uid"] or 0
        print("决策 #%s [%s] -> %s" % (d["id"], d["trigger"], d["draft"]))
    if not text:
        print("没有可发送的内容（用 --decision 或 --text）")
        return 1

    cookies, meta = load_cookies(args.cookie_file)
    print("发送账号: %s (uid=%s)" % (meta.get("uname"), meta.get("uid")))
    print("房间    : %s" % args.room)
    print("内容    : %s" % text)
    print("回复对象: %s" % (reply_mid or "无"))
    if not args.yes:
        print("\n这是预览。加 --yes 才会真的发送。")
        return 0

    res = send_danmaku(cookies, args.room, text, reply_mid=reply_mid)
    print("\n结果: code=%s %s" % (res.get("code"), res.get("message")))
    if res.get("reply_mid"):
        print("      回复关系 reply_mid=%s reply_uname=%r type=%s" % (
            res.get("reply_mid"), res.get("reply_uname"),
            res.get("reply_type_enum")))
    return 0 if res.get("code") == 0 else 1


def cmd_watch(args):
    """监听模式：实时看弹幕，触发则生成回复（--send 时自动发送）。"""
    import threading
    from .llm import LLM as _LLM
    from .watch import Watcher, tail

    cfg = args.cfg
    api_key = cfg["llm"].get("api_key")
    llm = _LLM(cfg) if api_key else NullLLM()
    cfg["_db"] = args.db
    w = Watcher(cfg, args.room, args.danmaku_log, llm,
                auto_send=args.send, cookie_file=args.cookie_file,
                enable_index=not args.no_index,
                transcript_log=args.transcript_log)
    if args.seconds:
        deadline = time.time() + args.seconds
        stop = lambda: time.time() > deadline
    else:
        stop = None
    print("模型: %s | 自动发送: %s | 触发词: %s" % (
        llm.name, "开" if args.send else "关（只看不发）",
        cfg["triggers"].get("keywords")))
    stats = tail(w, stop)
    print("统计: %s" % json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_index(args):
    """把历史内容建成向量索引（语义检索用）。"""
    from .embed import Embedder, VectorIndex

    store = _store(args)
    print("加载 embedding 模型 ...")
    emb = Embedder()
    idx = VectorIndex(store, emb)
    n = idx.build()
    total = store.conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    print("本次新增 %d 条，索引共 %d 条" % (n, total))
    return 0


def cmd_clean(args):
    """把早期未过滤的噪声标出来（幻觉、英文歌词、重复句），并重建索引。"""
    from . import clean

    store = _store(args)
    rows = store.conn.execute(
        "SELECT id, text FROM events WHERE is_noise=0 AND text IS NOT NULL"
    ).fetchall()
    marked, reasons = 0, {}
    with store.conn:
        for rid, text in rows:
            why = clean.is_noise_text(text)
            if why:
                store.conn.execute("UPDATE events SET is_noise=1 WHERE id=?",
                                   (rid,))
                reasons[why] = reasons.get(why, 0) + 1
                marked += 1
        # 重复句：同一文本出现 3 次以上，只留最早一条
        dup = store.conn.execute(
            """SELECT text, COUNT(*) c, MIN(id) keep FROM events
               WHERE is_noise=0 AND text IS NOT NULL
               GROUP BY text HAVING c>=3""").fetchall()
        for text, cnt, keep in dup:
            store.conn.execute(
                "UPDATE events SET is_noise=1 WHERE text=? AND id<>?",
                (text, keep))
            marked += cnt - 1
            reasons["duplicate"] = reasons.get("duplicate", 0) + cnt - 1
    print("标记噪声 %d 条" % marked)
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("  %-22s %d" % (k, v))
    # 噪声不进索引，清掉旧向量重新建
    store.conn.execute(
        "DELETE FROM vectors WHERE ref_id IN (SELECT id FROM events "
        "WHERE is_noise=1)")
    store.conn.commit()
    print("已清理对应向量，剩余索引 %d 条" % store.conn.execute(
        "SELECT COUNT(*) FROM vectors").fetchone()[0])
    return 0


def cmd_kb(args):
    """构建知识库：事实卡 + 人物卡。"""
    from . import kb as kbm
    from .llm import LLM as _LLM

    store = _store(args)
    llm = _LLM(args.cfg)
    room = args.room or 123456789
    if args.what in ("facts", "all"):
        print("=== 归纳直播间事实卡 ===")
        data = kbm.build_room_facts(store, llm, room)
        if data:
            print(json.dumps(data, ensure_ascii=False, indent=2))
    if args.what in ("people", "all"):
        print()
        print("=== 归纳观众人物卡 ===")
        n = kbm.build_people(store, llm, limit=args.limit,
                             min_msg=args.min_msg)
        print("完成 %d 人" % n)
    return 0


def cmd_web(args):
    """启动实时监控前端（同时可挂监听与自动回复）。"""
    from .llm import LLM as _LLM
    from .store import Store as _Store
    from .web import LiveConsole, serve

    cfg = args.cfg
    cfg["_db"] = args.db
    llm = _LLM(cfg) if cfg["llm"].get("api_key") else NullLLM()
    store = _Store(args.db)
    console = LiveConsole(cfg, args.room, store, llm,
                          auto_send=args.send,
                          cookie_file=args.cookie_file,
                          danmaku_log=args.danmaku_log,
                          transcript_log=args.transcript_log,
                          backfill=args.backfill,
                          decide_enabled=not args.monitor_only,
                          # 纯监控模式不跑决策，检索也没人用，省掉模型加载；
                          # 但 --kb 打开知识库写入时，检索仍要用来补索引
                          enable_index=(not args.no_index
                                        and (not args.monitor_only
                                             or args.kb)),
                          # --kb 显式打开知识库维护（纯监控模式下默认关闭）
                          kb_enabled=(True if args.kb else None))
    console.start()
    if args.monitor_only:
        mode = "纯监控（不生成回复、不发送）"
        if args.kb:
            mode += "＋知识库写入"
    elif args.send:
        mode = "自动回复（生成+发送）"
    else:
        mode = "影子模式（生成候选但不发送）"
    print("模型: %s | 模式: %s | 会话: %s" % (
        llm.name, mode, console.session_id))
    if args.danmaku_log:
        print("监听弹幕日志: %s" % args.danmaku_log)
    if args.transcript_log:
        print("监听语音日志: %s" % args.transcript_log)
    serve(console, port=args.port, host=args.host)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="agent", description="直播情报库与影子决策")
    p.add_argument("--config", default=None, help="配置文件，默认 config.yaml")
    p.add_argument("--db", default=None, help="SQLite 路径（默认取配置里的 db）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="导入抓取产物")
    s.add_argument("--danmaku")
    s.add_argument("--transcript")
    s.add_argument("--room", type=int)
    s.add_argument("--date", help="YYYY-MM-DD，默认取文件修改日期")
    s.add_argument("--session")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("stats", help="场次概览")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("users", help="活跃用户")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_users)

    s = sub.add_parser("search", help="全文检索")
    s.add_argument("--query", required=True)
    s.add_argument("--session")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("replay", help="回放并跑影子决策")
    s.add_argument("--session")
    s.add_argument("--start", type=float, help="起始 offset 秒")
    s.add_argument("--end", type=float, help="结束 offset 秒")
    s.add_argument("--offline", action="store_true", help="用离线占位模型")
    s.add_argument("--model")
    s.add_argument("--api-key")
    s.add_argument("--base-url")
    s.add_argument("--persona-file")
    s.add_argument("--bot-name", default="")
    s.add_argument("--keywords")
    s.add_argument("--min-interval", type=float, default=30)
    s.add_argument("--no-index", action="store_true",
                   help="不加载向量索引（默认加载，保证回放与线上一致）")
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("decisions", help="查看决策记录")
    s.add_argument("--session")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_decisions)

    s = sub.add_parser("send", help="发送一条决策或指定文本（需 --yes）")
    s.add_argument("--decision", type=int, help="要发送的 decision id")
    s.add_argument("--text", help="直接指定内容")
    s.add_argument("--room", type=int, required=True)
    s.add_argument("--reply-mid", type=int, default=0)
    s.add_argument("--cookie-file", default=str(HERE.parent / "bili_cookies.json"))
    s.add_argument("--yes", action="store_true", help="确认发送")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("watch", help="监听模式：实时看弹幕并回复")
    s.add_argument("--room", type=int, required=True)
    s.add_argument("--danmaku-log", required=True,
                   help="bili_danmaku.ps1 正在写的日志文件")
    s.add_argument("--transcript-log",
                   help="bili_live_asr.py 正在写的转录日志（给了才处理主播语音）")
    s.add_argument("--send", action="store_true", help="通过护栏后自动发送")
    s.add_argument("--seconds", type=int, default=0, help="运行时长，0=一直跑")
    s.add_argument("--no-index", action="store_true",
                   help="关闭语义检索（默认开启，走本地向量索引）")
    s.add_argument("--cookie-file",
                   default=str(HERE.parent / "bili_cookies.json"))
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("web", help="实时监控前端（看弹幕/看主播说话/手动发弹幕）")
    s.add_argument("--room", type=int, required=True)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--danmaku-log", help="bili_danmaku.ps1 正在写的日志")
    s.add_argument("--transcript-log", help="bili_live_asr.py 正在写的转录")
    s.add_argument("--send", action="store_true", help="开启机器人自动回复")
    s.add_argument("--monitor-only", action="store_true",
                   help="纯监控：连触发判定与模型调用都不跑，只采集和展示")
    s.add_argument("--kb", action="store_true",
                   help="纯监控模式下也跑知识库维护（事实抽取/摘要/索引/人物卡）")
    s.add_argument("--backfill", type=int, default=80,
                   help="启动时回填日志末尾若干行，避免界面空白（不回触发决策）")
    s.add_argument("--no-index", action="store_true",
                   help="关闭语义检索（默认开启，走本地向量索引）")
    s.add_argument("--cookie-file",
                   default=str(HERE.parent / "bili_cookies.json"))
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("index", help="建立向量索引（语义检索用）")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("clean", help="标记早期入库的噪声并清理索引")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("kb", help="构建知识库（事实卡 / 人物卡）")
    s.add_argument("--what", default="all",
                   choices=["facts", "people", "all"])
    s.add_argument("--room", type=int, default=123456789)
    s.add_argument("--limit", type=int, default=20,
                   help="归纳多少个活跃观众")
    s.add_argument("--min-msg", type=int, default=5,
                   help="发言少于这个数的不归纳")
    s.set_defaults(func=cmd_kb)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    args.cfg = cfg
    if not args.db:
        db = cfg.get("db") or "data/events.db"
        args.db = db if os.path.isabs(db) else str(HERE.parent / db)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
