"""把已有的抓取产物导入情报库。

支持两种日志格式（都是本项目自己的脚本产出的）：
  bili_danmaku.ps1  ->  [HH:MM:SS] [弹幕] 昵称 | uid=1 | medal=粉丝牌 lv=21 | 内容
  bili_live_asr.py  ->  [HH:MM:SS] 转写文本   (处理 0.8s | 端到端延迟 -27.5s)
"""

import datetime as dt
import re
from pathlib import Path

from . import clean

DANMAKU_NEW = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+\[弹幕\]\s+(?P<name>.*?)\s+\|\s+uid=(?P<uid>-?\d+)"
    r"\s+\|\s+medal=(?P<medal>.*?)\s+lv=(?P<lv>\d*)\s+\|\s+"
    r"(?:reply=(?P<rmid>-?\d+):(?P<rname>[^|]*?)\s+\|\s+)?"
    r"(?P<text>.*)$")
DANMAKU_OLD = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+\[弹幕\]\s+(?P<name>[^:]+):\s+(?P<text>.*)$")
# 礼物行有两种写法：
#   旧： [HH:MM:SS] [礼物] 昵称 送出 辣条 x1
#   新： [HH:MM:SS] [礼物] 昵称 | uid=123 | 送出 辣条 x1
# 只认旧写法会把新日志里的昵称解析成「昵称 | uid=... |」，并且丢掉 uid。
GIFT_RE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+\[礼物\]\s+(?P<name>.*?)\s*"
    r"(?:\|\s*uid=(?P<uid>-?\d+)\s*\|)?\s*"
    r"送出\s+(?P<gift>.*?)\s+x(?P<n>\d+)$")
SPEECH_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+(?P<text>.*?)(?:\s+\(处理.*)?$")
# 转录脚本自己的控制行（断流缺口/心跳等），不是主播说的话，导入时跳过
SPEECH_CONTROL_PREFIXES = ("[缺口]", "[心跳]", "[状态]", "[重连]", "[错误]",
                           "[警告]", "[测试]")
SESSION_HDR = re.compile(r"#\s*直播间\s+(\d+)\s+弹幕记录开始于\s+(\S+ \S+)")


def _ts(date: dt.date, hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return int(dt.datetime.combine(
        date, dt.time(h, m, s)).timestamp() * 1000)


def parse_danmaku_file(path: Path, date: dt.date):
    """返回 (room_id, start_date_str, 事件列表)。"""
    room_id, started = None, None
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SESSION_HDR.search(line)
        if m:
            room_id = int(m.group(1))
            started = m.group(2)
            continue
        m = DANMAKU_NEW.match(line)
        if m:
            medal = (m.group("medal") or "").strip()
            lv = m.group("lv") or 0
            meta = {"medal_name": medal, "medal_level": int(lv or 0)}
            if m.group("rmid"):
                meta["reply_mid"] = int(m.group("rmid"))
                meta["reply_uname"] = (m.group("rname") or "").strip()
            events.append({
                "kind": "danmaku",
                "ts": _ts(date, m.group(1)),
                "uid": int(m.group("uid")),
                "uname": m.group("name").strip(),
                "text": m.group("text").strip(),
                "meta": meta,
            })
            continue
        m = DANMAKU_OLD.match(line)
        if m:
            events.append({
                "kind": "danmaku", "ts": _ts(date, m.group(1)),
                "uid": None, "uname": m.group("name").strip(),
                "text": m.group("text").strip(), "meta": {},
            })
            continue
        m = GIFT_RE.match(line)
        if m:
            events.append({
                "kind": "gift", "ts": _ts(date, m.group(1)),
                "uid": int(m.group("uid")) if m.group("uid") else None,
                "uname": m.group("name").strip(),
                "text": "送出 %s x%s" % (m.group("gift"), m.group("n")), "meta": {},
            })
    return room_id, started, events


def parse_transcript_file(path: Path, date: dt.date):
    """解析语音转录日志。"""
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SPEECH_RE.match(line.strip())
        if not m:
            continue
        text = (m.group("text") or "").strip()
        if (not text or text.startswith("#")
                or text.startswith(SPEECH_CONTROL_PREFIXES)):
            continue
        events.append({
            "kind": "speech", "ts": _ts(date, m.group(1)),
            "uid": None, "uname": "主播", "text": text, "meta": {},
        })
    return events


def ingest(store, danmaku_path=None, transcript_path=None, room_id=None,
           date=None, session_id=None, cfg=None):
    """导入并返回 (session_id, 统计)。"""
    cfg = cfg or {}
    clean.set_bot_patterns((cfg.get("clean") or {}).get("bot_reply_patterns"))
    files = [Path(p) for p in (danmaku_path, transcript_path) if p]
    if not files:
        raise SystemExit("至少要给一个 --danmaku 或 --transcript")
    if date is None:
        date = dt.date.fromtimestamp(files[0].stat().st_mtime)

    events, started = [], None
    if danmaku_path:
        rid, started, evs = parse_danmaku_file(Path(danmaku_path), date)
        room_id = room_id or rid
        events += evs
    if transcript_path:
        events += parse_transcript_file(Path(transcript_path), date)

    if not events:
        return None, {"events": 0}

    events.sort(key=lambda e: e["ts"])
    base = events[0]["ts"]
    sid = session_id or "%s_%s" % (room_id or "unknown",
                                   dt.datetime.fromtimestamp(base / 1000)
                                   .strftime("%Y%m%d_%H%M%S"))
    store.ensure_session(sid, room_id or 0, start_ts=base)

    kept, dropped, bots = 0, {}, 0
    for e in events:
        is_bot = 0
        if e["kind"] == "danmaku":
            if (clean.is_bot_author(e.get("uid"), e.get("uname"), cfg)
                    or clean.is_bot_reply(e["text"])):
                is_bot = 1
        norm, why = clean.normalize_text(e["text"], kind=e["kind"],
                                         is_bot=bool(is_bot))
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        store.add_event(sid, e["ts"], (e["ts"] - base) / 1000.0, e["kind"],
                        text=e["text"], uid=e.get("uid"),
                        uname=e.get("uname"), text_norm=norm,
                        meta=e.get("meta"), is_bot=is_bot)
        if is_bot:
            bots += 1
        elif e["kind"] == "danmaku" and e.get("uid"):
            m = e.get("meta") or {}
            store.upsert_user(e["uid"], e.get("uname"), e["ts"],
                              m.get("medal_name"), m.get("medal_level"))
        kept += 1

    store.conn.execute(
        "UPDATE sessions SET danmaku_count=(SELECT COUNT(*) FROM events "
        "WHERE session_id=? AND type='danmaku'), note=? WHERE id=?",
        (sid, "started=%s" % started, sid))
    store.conn.commit()
    return sid, {"events": len(events), "kept": kept, "bot": bots,
                 "dropped": dropped}
