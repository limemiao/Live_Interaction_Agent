"""监听模式：把整套机制实时挂在直播间上，边看边回。

数据源复用已经很稳的抓取脚本 bili_danmaku.ps1——它逐行 flush 写日志，
这里 tail 这个文件即可，不需要另写 WebSocket 客户端。

流程：新弹幕 -> 落库 -> 触发判定 -> LLM 生成 -> 护栏 -> 发送 -> 审计
"""

import re
import threading
import time
from datetime import date, datetime
from pathlib import Path

from . import clean
from .decide import SendState, decide_one
from .embed import open_index
from .execute import load_cookies, send_danmaku
from .queue import SendItem, SendQueue
from .store import Store

DANMAKU_RE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+\[弹幕\]\s+(?P<name>.*?)\s+\|\s+uid=(?P<uid>-?\d+)"
    r"\s+\|\s+medal=(?P<medal>.*?)\s+lv=(?P<lv>\d*)\s+\|\s+"
    r"(?:reply=(?P<rmid>-?\d+):(?P<rname>[^|]*?)\s+\|\s+)?"
    r"(?P<text>.*)$")

# bili_live_asr.py 的转录行：[HH:MM:SS] 文本（后面可能跟处理的耗时标注）
SPEECH_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+(?P<text>.*?)(?:\s+\(处理.*)?$")


def ts_from_hms(hms, day=None):
    """把日志里的 HH:MM:SS 换算成毫秒时间戳（按当天算）。"""
    day = day or date.today()
    h, mi, s = (int(x) for x in hms.split(":"))
    ts = int(datetime.combine(day, datetime.min.time()).timestamp() * 1000)
    return ts + (h * 3600 + mi * 60 + s) * 1000


def parse_line(line, day=None):
    m = DANMAKU_RE.match(line.lstrip("\ufeff").strip())
    if not m:
        return None
    day = day or date.today()
    h, mi, s = (int(x) for x in m.group(1).split(":"))
    ts = int(datetime.combine(day, datetime.min.time()).timestamp() * 1000)
    ts += (h * 3600 + mi * 60 + s) * 1000
    medal = (m.group("medal") or "").strip()
    lv = int(m.group("lv") or 0)
    out = {
        "ts": ts,
        "uid": int(m.group("uid")),
        "uname": m.group("name").strip(),
        "text": m.group("text").strip(),
        "meta": {"medal_name": medal, "medal_level": lv},
    }
    if m.group("rmid"):
        out["meta"]["reply_mid"] = int(m.group("rmid"))
        out["meta"]["reply_uname"] = (m.group("rname") or "").strip()
    return out


def live_session_id(room_id):
    # 带上启动时刻，避免上一次运行的弹幕留在 180 秒窗口里污染上下文
    return "live_%s_%s_%s" % (room_id, date.today().strftime("%Y%m%d"),
                              time.strftime("%H%M%S"))


class Watcher:
    def __init__(self, cfg, room_id, log_path, llm, auto_send=False,
                 cookie_file=None, verbose=True, enable_index=True,
                 transcript_log=None):
        self.cfg = cfg
        self.room_id = room_id
        self.log_path = Path(log_path)
        # bili_live_asr.py 写的转录日志；给了它才处理主播语音
        self.transcript_log = Path(transcript_log) if transcript_log else None
        self.llm = llm
        self.auto_send = auto_send
        self.cookie_file = cookie_file
        self.verbose = verbose
        self.store = Store(cfg.get("_db") or "data/events.db")
        self.session_id = live_session_id(room_id)
        self.base_ts = int(time.time() * 1000)
        self.store.ensure_session(self.session_id, room_id,
                                  title="实时监听", start_ts=self.base_ts)
        self.state = SendState()
        self.last_sent_ts = None
        self.stats = {"danmaku": 0, "speech": 0, "triggered": 0, "sent": 0,
                      "queued": 0, "expired": 0, "rejected": 0, "error": 0}
        self._cookies = None
        self.index = open_index(self.store, log=self._say, enabled=enable_index)
        # 自己发言的回声识别（未开播房间回显的 uid=0、昵称打码）
        self.echo = clean.OwnEcho()
        scfg = (cfg.get("send") or {})
        self.sender = SendQueue(
            send_fn=self._send_fn,
            min_interval_s=scfg.get("min_interval_s", 3),
            per_minute=scfg.get("per_minute", 8),
            ttl_s=scfg.get("ttl_s", 60),
            log=lambda m: self._say("[队列] " + m),
        )
        self.sender.on_sent = self._on_sent
        self.sender.on_expire = self._on_expire

    def _say(self, msg):
        if self.verbose:
            print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

    def _get_cookies(self):
        if self._cookies is None:
            self._cookies = load_cookies(self.cookie_file)[0]
        return self._cookies

    def handle(self, ev):
        """处理一条新弹幕：落库 + 决策 + 可选发送。"""
        self.stats["danmaku"] += 1
        is_bot = 1 if (clean.is_bot_author(ev["uid"], ev["uname"], self.cfg)
                       or clean.is_bot_reply(ev["text"])
                       or self.echo.is_echo(ev["text"], ev["ts"])) else 0
        norm, why = clean.normalize_text(ev["text"], is_bot=bool(is_bot))
        if why:
            return
        self.store.add_event(
            self.session_id, ev["ts"], (ev["ts"] - self.base_ts) / 1000.0,
            "danmaku", text=ev["text"], uid=ev["uid"], uname=ev["uname"],
            text_norm=norm, meta=ev.get("meta"), is_bot=is_bot)
        if is_bot:
            return
        self.store.upsert_user(ev["uid"], ev["uname"], ev["ts"],
                               (ev.get("meta") or {}).get("medal_name"),
                               (ev.get("meta") or {}).get("medal_level"))

        event = {"ts": ev["ts"], "type": "danmaku", "text": norm,
                 "uid": ev["uid"], "uname": ev["uname"], "is_bot": 0,
                 "session_id": self.session_id, "meta": ev.get("meta")}
        res = decide_one(self.store, self.llm, self.session_id, event,
                         cfg=self.cfg, state=self.state, index=self.index,
                         room_id=self.room_id)
        if not res:
            return
        self.stats["triggered"] += 1
        act = res.get("action")
        if act == "shadow":
            if self.auto_send:
                self.sender.put(SendItem(res["draft"], reply_mid=ev["uid"],
                                         decision_id=res.get("decision_id"),
                                         trigger=res["trigger"]))
                self.stats["queued"] += 1
            else:
                self._say("候选(未发送) [%s] %s" % (res["trigger"], res["draft"]))
        elif act == "rejected_by_guard":
            self.stats["rejected"] += 1
            self._say("护栏拦下 [%s] %s  (%s)" % (
                res["trigger"], res["draft"], res["reason"]))
        elif act == "error":
            self.stats["error"] += 1
            self._say("模型出错: %s" % res.get("reason"))

    def _send_fn(self, item):
        """真正执行发送（由队列按限速调用）。"""
        # 发送前登记内容，WS 回显比接口返回先到也认得出
        self.echo.note(item.text, time.time() * 1000)
        cookies = self._get_cookies()
        return send_danmaku(cookies, self.room_id, item.text,
                            reply_mid=item.reply_mid)

    def handle_speech(self, ev):
        """处理一条主播语音转写：落库 + 决策。

        detect_trigger 对 speech 只认"叫到名字"，所以这里不会每句话都触发，
        但它承载 anchor_call 这条通道（语音侧的工具链：add_room_fact /
        set_topic / add_slang），必须接进来，否则只有回放器跑得到。
        """
        self.stats["speech"] += 1
        norm, why = clean.normalize_text(ev["text"], kind="speech")
        if why:
            return None
        self.store.add_event(
            self.session_id, ev["ts"], (ev["ts"] - self.base_ts) / 1000.0,
            "speech", text=ev["text"], uname="主播", text_norm=norm)
        event = {"ts": ev["ts"], "type": "speech", "text": norm, "uid": None,
                 "uname": "主播", "is_bot": 0, "session_id": self.session_id}
        res = decide_one(self.store, self.llm, self.session_id, event,
                         cfg=self.cfg, state=self.state, index=self.index,
                         room_id=self.room_id)
        if not res:
            return None
        self.stats["triggered"] += 1
        act = res.get("action")
        if act == "shadow":
            if self.auto_send:
                self.sender.put(SendItem(res["draft"],
                                         decision_id=res.get("decision_id"),
                                         trigger=res["trigger"]))
                self.stats["queued"] += 1
            else:
                self._say("候选(未发送) [%s] %s" % (res["trigger"], res["draft"]))
        elif act == "rejected_by_guard":
            self.stats["rejected"] += 1
            self._say("护栏拦下 [%s] %s  (%s)" % (
                res["trigger"], res["draft"], res["reason"]))
        elif act == "error":
            self.stats["error"] += 1
            self._say("模型出错: %s" % res.get("reason"))
        return res

    def _on_sent(self, item, out):
        if out.get("code") == 0:
            self.stats["sent"] += 1
            if item.decision_id:
                self.store.update_decision(item.decision_id, action="sent")
            self._say("已回复: %s  (reply_mid=%s)" % (item.text,
                                                     out.get("reply_mid")))
        else:
            self.stats["error"] += 1
            if item.decision_id:
                self.store.update_decision(
                    item.decision_id, action="send_failed",
                    reason="code=%s %s" % (out.get("code"), out.get("message")))

    def _on_expire(self, item):
        self.stats["expired"] += 1
        if item.decision_id:
            self.store.update_decision(item.decision_id, action="expired",
                                       reason="排队超过 TTL 未发送")


def _tail_speech(watcher, stop_when=None):
    """tail 语音转录日志，逐行交给 watcher.handle_speech。

    和弹幕 tail 放在两条线程里：语音转录是"每句话一行"，弹幕是"每条一行"，
    串行读会让一边的卡顿影响到另一边。
    """
    path = watcher.transcript_log
    watcher._say("监听语音 %s" % path)
    while not path.exists():
        if stop_when and stop_when():
            return
        time.sleep(0.5)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                if stop_when and stop_when():
                    break
                time.sleep(0.4)
                continue
            m = SPEECH_RE.match(line.lstrip("\ufeff").strip())
            if not m:
                continue
            text = (m.group("text") or "").strip()
            if not text or text.startswith("#"):
                continue
            try:
                watcher.handle_speech({"ts": ts_from_hms(m.group(1)),
                                       "text": text})
            except Exception as exc:
                watcher._say("语音处理出错: %s: %s" % (type(exc).__name__, exc))


def _kb_loop(watcher, stop_when=None):
    """定时做知识库维护：抽取直播间事实/话题 + 更新本场摘要。

    两件事都不需要"主播叫机器人名字"，所以主播正常说话也能积累情报。
    素材不够就跳过，安静时没有任何模型调用。
    """
    ccfg = watcher.cfg.get("context") or {}
    interval = float(ccfg.get("summary_interval_s", 300))
    if interval <= 0:
        return
    from . import extract, kb
    min_lines = int(ccfg.get("extract_min_lines", 20))
    max_facts = int(ccfg.get("extract_max_facts", 3))
    daily = int(ccfg.get("extract_daily_limit", 40))
    min_events = int(ccfg.get("summary_min_events", 40))
    keep = int(ccfg.get("summary_keep_chars", 2000))
    people_every = int(ccfg.get("people_refresh_every", 6))
    people_max = int(ccfg.get("people_refresh_max", 3))
    people_stale = float(ccfg.get("people_stale_hours", 12))
    people_once = bool(ccfg.get("people_once_per_session", False))
    people_done_key = "people_done:" + watcher.session_id
    tick = 0
    while True:
        time.sleep(interval)
        if stop_when and stop_when():
            return
        tick += 1
        try:
            n = extract.maybe_extract_room_facts(
                watcher.store, watcher.llm, watcher.session_id,
                watcher.room_id, min_lines=min_lines, max_facts=max_facts,
                daily_limit=daily, verbose=True)
            if n:
                watcher._say("知识库：抽取入库 %d 条" % n)
        except Exception as exc:
            watcher._say("事实抽取失败: %s: %s" % (type(exc).__name__, exc))
        try:
            text = kb.maybe_update_summary(
                watcher.store, watcher.llm, watcher.session_id,
                min_events=min_events, keep_chars=keep)
            if text:
                watcher._say("本场摘要已更新（新增 %d 字）" % len(text))
        except Exception as exc:
            watcher._say("摘要更新失败: %s: %s" % (type(exc).__name__, exc))
        if watcher.index is not None:
            try:
                added = watcher.index.build(verbose=False, limit=500)
                if added:
                    watcher._say("向量索引补了 %d 条" % added)
            except Exception as exc:
                watcher._say("补索引失败: %s: %s" % (type(exc).__name__, exc))
        if people_once:
            if not watcher.store.get_setting(people_done_key):
                try:
                    n = kb.build_people(
                        watcher.store, watcher.llm, limit=60, min_msg=5,
                        # 本场一次：不按 stale 跳过（理由见 web._kb_loop）
                        stale_hours=None,
                        max_people=(people_max or None), verbose=False)
                    watcher.store.set_setting(people_done_key, "1")
                    watcher._say("人物卡更新 %d 张（本场一次）" % n)
                except Exception as exc:
                    watcher._say("人物卡更新失败: %s: %s"
                                 % (type(exc).__name__, exc))
        elif people_every > 0 and tick % people_every == 0:
            try:
                n = kb.build_people(watcher.store, watcher.llm, limit=60,
                                    min_msg=5, stale_hours=people_stale,
                                    max_people=people_max, verbose=False)
                if n:
                    watcher._say("人物卡更新 %d 张" % n)
            except Exception as exc:
                watcher._say("人物卡更新失败: %s: %s"
                             % (type(exc).__name__, exc))


def tail(watcher, stop_when=None):
    """tail 弹幕日志文件，逐行交给 watcher 处理。"""
    path = watcher.log_path
    watcher._say("监听 %s（房间 %s）" % (path, watcher.room_id))
    if watcher.transcript_log:
        threading.Thread(target=_tail_speech, args=(watcher, stop_when),
                         daemon=True).start()
    threading.Thread(target=_kb_loop, args=(watcher, stop_when),
                     daemon=True).start()
    if watcher.auto_send:
        watcher.sender.start()
        scfg = watcher.cfg.get("send") or {}
        watcher._say("发送队列已启动：间隔 ≥%ss，每分钟 ≤%s 条，TTL %ss" % (
            scfg.get("min_interval_s", 3), scfg.get("per_minute", 8),
            scfg.get("ttl_s", 60)))
    while not path.exists():
        time.sleep(0.5)
    f = path.open("r", encoding="utf-8", errors="replace")
    f.seek(0, 2)                       # 只看新增内容
    try:
        while True:
            line = f.readline()
            if not line:
                if stop_when and stop_when():
                    break
                time.sleep(0.4)
                continue
            ev = parse_line(line)
            if ev:
                try:
                    watcher.handle(ev)
                except Exception as exc:
                    # 单条弹幕处理失败不能拖垮整个监听器
                    watcher._say("处理出错: %s: %s" % (type(exc).__name__, exc))
    except KeyboardInterrupt:
        pass
    finally:
        f.close()
        if watcher.auto_send:
            watcher.sender.stop()
    return watcher.stats
