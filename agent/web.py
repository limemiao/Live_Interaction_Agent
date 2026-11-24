"""实时监控前端：看主播说了啥、看弹幕、手动发弹幕。

只用标准库（http.server + SSE），不引入 Web 框架。

一个进程里同时跑：
  弹幕 tail   -> 落库 -> 触发判定 -> 生成 -> 发送队列
  语音 tail   -> 落库（供界面展示）
  HTTP 服务   -> SSE 把新事件推给浏览器；POST /send 手动发弹幕

这样做的原因是发送队列必须唯一——如果前端和监听器是两个进程，
各自维护一套"刚发过几条"的状态，合计就会突破限速。
"""

import json
import queue
import re
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import clean
from .decide import SendState, decide_one
from .embed import open_index
from .execute import load_cookies, send_danmaku
from .queue import SendItem, SendQueue
from .store import Store
from . import pages
from .jobs import TranscribeJob, list_recordings

HERE = Path(__file__).resolve().parent
ROOT_AUDIO = HERE.parent / "audio"

DANMAKU_RE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+\[弹幕\]\s+(?P<name>.*?)\s+\|\s+uid=(?P<uid>-?\d+)"
    r"\s+\|\s+medal=(?P<medal>.*?)\s+lv=(?P<lv>\d*)\s+\|\s+"
    r"(?:reply=(?P<rmid>-?\d+):(?P<rname>[^|]*?)\s+\|\s+)?"
    r"(?P<text>.*)$")
SPEECH_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+(?P<text>.*?)(?:\s+\(处理.*)?$")
# 礼物 / SC / 进入：抓取脚本写的是同一族格式（带 uid）。
# 以前只解析 [弹幕] 行，所以"礼物触发"在实时链路上永远不会发生——
# 配置里 types 写了 gift/superchat，实际一条都收不到。
OTHER_RE = re.compile(
    r"^\[(?P<hms>\d{2}:\d{2}:\d{2})\]\s+\[(?P<kind>礼物|SC|进入)\]\s+"
    r"(?P<uname>.*?)\s*\|\s*uid=(?P<uid>-?\d+)\s*\|\s*(?P<text>.*)$")
OTHER_KINDS = {"礼物": "gift", "SC": "superchat", "进入": "enter"}

# 转录脚本自己写的控制行。它们也带 `[HH:MM:SS]` 前缀，会命中 SPEECH_RE，
# 不排除的话 `[缺口] ffmpeg 退出...` 会被当成主播说的一句话入库，
# 进而污染摘要、事实抽取和人物卡。
SPEECH_CONTROL_PREFIXES = ("[缺口]", "[心跳]", "[状态]", "[重连]", "[错误]",
                           "[警告]", "[测试]")


def _ts_from_hms(hms, day=None):
    day = day or date.today()
    h, m, s = (int(x) for x in hms.split(":"))
    base = int(datetime.combine(day, datetime.min.time()).timestamp() * 1000)
    return base + (h * 3600 + m * 60 + s) * 1000


class Hub:
    """把新事件广播给所有浏览器连接。"""

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, item):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass


class LiveConsole:
    def __init__(self, cfg, room_id, store, llm, auto_send=False,
                 cookie_file=None, danmaku_log=None, transcript_log=None,
                 backfill=0, decide_enabled=True, enable_index=True,
                 kb_enabled=None):
        self.cfg = cfg
        self.room_id = room_id
        self.store = store
        self.llm = llm
        self.auto_send = auto_send
        # decide_enabled=False 时完全不跑触发判定与模型调用（纯观测）
        # decide_enabled=True 且 auto_send=False 时是影子模式：生成但不发送
        self.decide_enabled = decide_enabled
        # 知识库维护（事实抽取 / 摘要 / 索引 / 人物卡）独立于"要不要回复"：
        # 纯监控模式默认仍然不跑模型，但可以用 --kb 显式打开写入。
        # 以前它直接绑在 decide_enabled 上，导致 --monitor-only 时知识库静默停更。
        self.kb_enabled = decide_enabled if kb_enabled is None else bool(kb_enabled)
        self.cookie_file = cookie_file
        self.danmaku_log = Path(danmaku_log) if danmaku_log else None
        self.transcript_log = Path(transcript_log) if transcript_log else None
        self.backfill = max(0, int(backfill or 0))
        self._backfilling = False
        self.hub = Hub()
        self.session_id = "live_%s_%s" % (
            room_id, datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.base_ts = int(time.time() * 1000)
        self.state = SendState()
        self.stats = {"danmaku": 0, "speech": 0, "triggered": 0,
                      "queued": 0, "sent": 0, "expired": 0, "error": 0}
        self._cookies = None
        self._cookies_by_account = {}
        # 自己发言的"回声识别"：未开播房间回显的弹幕 uid=0、昵称打码，
        # 光靠 uid/昵称认不出自己，会变成自问自答
        self.echo = clean.OwnEcho()
        # 语义检索：拿不到索引（模型缺失/显存不足）就退化成"只靠近场上下文"，
        # 不影响监听和回复。少了它模型就只能看见最近 3 分钟，看不见往期。
        self.index = open_index(store, log=self._log, enabled=enable_index)
        _root = Path(__file__).resolve().parent.parent
        self.accounts = {
            "bot": str(cookie_file or (_root / "bili_cookies.json")),
            "operator": str(_root / "bili_cookies_operator.json"),
        }
        self._stop = threading.Event()
        # --- 看门狗：两台采集进程死了要能发现，而不是"界面上没动静" ---------
        # 判据刻意保守：只在"之前有动静、后来停了"时才报。房间没开播、
        # 或者本来就没观众说话的静默是正常的，无法区分，所以不报（避免狼来了）。
        self.watchdog_cfg = cfg.get("watchdog") or {}
        self.last_danmaku_ts = None
        self.last_speech_ts = None
        self.alerts = []
        store.ensure_session(self.session_id, room_id, title="实时监控",
                             start_ts=self.base_ts)
        clean.set_bot_patterns((cfg.get("clean") or {}).get("bot_reply_patterns"))
        scfg = cfg.get("send") or {}
        self.sender = SendQueue(
            send_fn=self._send_fn,
            min_interval_s=scfg.get("min_interval_s", 3),
            per_minute=scfg.get("per_minute", 8),
            ttl_s=scfg.get("ttl_s", 60),
            log=self._log,
        )
        self.sender.on_sent = self._on_sent
        self.sender.on_expire = self._on_expire

    def _log(self, msg):
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)

    # ---------- 看门狗 ----------
    def _watchdog_log(self, line):
        """告警留一份到 run/watchdog.log，方便事后对时间线。"""
        try:
            path = Path(__file__).resolve().parent.parent / "run"
            path.mkdir(exist_ok=True)
            with (path / "watchdog.log").open("a", encoding="utf-8") as f:
                f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                     line))
        except Exception:
            pass

    def check_health(self):
        """返回当前的静默告警列表（给界面和 /stats 用）。

        判据是**日志文件多久没增长**（mtime），而不是"多久没解析到事件"：
        后者在"采集进程启动就崩了、一条都没送过来"时永远为空，等于哑巴
        （实测踩过：ASR 启动即崩，界面上毫无提示）。转录脚本会每 30 秒
        写一行 `# [心跳]`，弹幕脚本每 60 秒写一行状态，所以只要进程活着，
        文件就一定在增长。
        """
        if not self.watchdog_cfg.get("enabled", True):
            return []
        now = time.time()
        out = []
        checks = (
            ("danmaku", "弹幕抓取", self.danmaku_log,
             float(self.watchdog_cfg.get("danmaku_silence_s", 300))),
            ("speech", "语音转录", self.transcript_log,
             float(self.watchdog_cfg.get("speech_silence_s", 900))),
        )
        for key, label, path, limit in checks:
            if not path:
                continue
            p = Path(path)
            if not p.exists():
                continue          # 文件还没建 → 还没开始，不报
            age = now - p.stat().st_mtime
            if age > limit:
                out.append({
                    "source": key, "label": label, "silent_s": int(age),
                    "limit_s": int(limit),
                    "advice": "%s 的日志已 %d 秒没有增长，检查对应进程是否还活着"
                              % (label, int(age)),
                })
        return out

    def _watchdog_loop(self):
        if not self.watchdog_cfg.get("enabled", True):
            return
        interval = float(self.watchdog_cfg.get("check_interval_s", 30))
        seen = set()
        while not self._stop.wait(interval):
            try:
                alerts = self.check_health()
            except Exception as exc:
                self._log("[看门狗] 检查失败: %s" % exc)
                continue
            self.alerts = alerts
            keys = set(a["source"] for a in alerts)
            for a in alerts:
                if a["source"] not in seen:
                    self._log("[看门狗] %s 已静默 %d 秒（阈值 %d 秒）——%s"
                              % (a["label"], a["silent_s"], a["limit_s"],
                                 a["advice"]))
                    self._watchdog_log("ALERT %s silent=%ds limit=%ds"
                                       % (a["source"], a["silent_s"],
                                          a["limit_s"]))
            for k in seen - keys:
                self._log("[看门狗] %s 已恢复" % k)
                self._watchdog_log("RECOVER %s" % k)
            seen = keys

    # ---------- 发送 ----------
    def _cookies_or_load(self):
        if self._cookies is None:
            self._cookies = load_cookies(self.cookie_file)[0]
        return self._cookies

    def _cookies_for(self, account):
        """按身份取 Cookie：bot=示例机器人，operator=操作者 本人。"""
        if account not in self.accounts:
            account = "bot"
        if account not in self._cookies_by_account:
            self._cookies_by_account[account] = load_cookies(
                self.accounts[account])[0]
        return self._cookies_by_account[account]

    def _send_fn(self, item):
        account = getattr(item, "account", "bot")
        if account == "bot":
            # 发送前登记内容：WS 回显可能比接口返回先到，
            # 登记晚了就认不出自己那条
            self.echo.note(item.text, time.time() * 1000)
        return send_danmaku(self._cookies_for(account), self.room_id,
                            item.text, reply_mid=item.reply_mid)

    def _on_sent(self, item, out):
        ok = out.get("code") == 0
        if ok:
            self.stats["sent"] += 1
            self.state.note_sent(item.text)      # 手动发送的去重基线
            if item.decision_id:
                self.store.update_decision(item.decision_id, action="sent")
            self._log("已发送: %s" % item.text[:24])
        else:
            self.stats["error"] += 1
            self._log("发送失败 code=%s %s" % (out.get("code"),
                                              out.get("message")))
            if item.decision_id:
                self.store.update_decision(
                    item.decision_id, action="send_failed",
                    reason="code=%s %s" % (out.get("code"), out.get("message")))
        self.hub.publish({"kind": "send", "ok": ok, "text": item.text,
                          "manual": item.decision_id is None,
                          "message": out.get("message") or "",
                          "ts": int(time.time() * 1000)})

    def _on_expire(self, item):
        self.stats["expired"] += 1
        if item.decision_id:
            self.store.update_decision(item.decision_id, action="expired",
                                       reason="排队超过 TTL")

    def manual_send(self, text, reply_mid=0, account="bot"):
        """手动发弹幕：同样过队列，但 TTL 放宽到 5 分钟。"""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "message": "内容为空"}
        ok, reason = self._guard_ok(text, time.time() * 1000)
        if not ok:
            # 既要写日志，也要推到界面上——不然用户只看到"点了没反应"
            self._log("手动发送被拦下(%s): %s | %s" % (account, reason,
                                                     text[:24]))
            self.hub.publish({"kind": "send", "ok": False, "text": text,
                              "manual": True, "message": reason,
                              "ts": int(time.time() * 1000)})
            return {"ok": False, "message": reason}
        self.sender.put(SendItem(text, reply_mid=reply_mid, ttl_s=300,
                                 account=account))
        self._log("手动发送入队(%s): %s" % (account, text[:24]))
        return {"ok": True, "message": "已入队", "account": account}

    def _guard_ok(self, text, now_ts):
        """手动发送的**内容级**校验。

        **不做重复检查。** 手动发弹幕的是人，不是机器人：同一句话隔一会儿
        再说一遍是人的正常行为（B 站自己也有发言频率限制兜着）。去重是给
        机器人自己用的（别复读），不该管到人手上——实测已经因此误拦过两次：
          · "谢谢各位大哥"（3 分钟前发过）
          · "小助手，直播间刚才在聊什么内容"（56 秒前问过，相似度 0.81）
        所以这里只兜底长度 / 屏蔽词 / 身份泄露这三类"确实不该发出去"的内容。
        """
        from . import guard
        return guard.evaluate(text, now_ts, recent_texts=[],
                              duplicate_threshold=(self.cfg.get("guard")
                                                   or {}).get(
                                  "duplicate_threshold", 0.8))

    # ---------- 采集线程 ----------
    def _store_and_publish(self, kind, ts, text, uid=None, uname=None,
                           meta=None, is_bot=0, extra=None):
        norm, why = clean.normalize_text(text, kind=kind, is_bot=bool(is_bot))
        if why:
            # 以前这里直接 return，整条弹幕就消失了：库里没有、界面上也看不到，
            # 看起来像"抓不到完整弹幕"（实测丢的都是纯表情/单字弹幕）。
            # 现在照常入库并推给界面，只打上 is_noise 标记——
            # 检索、抽取、摘要、提示词都按 is_noise=0 过滤，信息质量不受影响。
            self.store.add_event(self.session_id, ts,
                                 (ts - self.base_ts) / 1000.0, kind,
                                 text=text, uid=uid, uname=uname,
                                 text_norm=text, meta=meta,
                                 is_bot=is_bot, is_noise=1)
            item = {"kind": kind, "ts": ts, "text": text, "uid": uid,
                    "uname": uname, "is_bot": is_bot, "noise": why}
            if extra:
                item.update(extra)
            self.hub.publish(item)
            return None
        self.store.add_event(self.session_id, ts, (ts - self.base_ts) / 1000.0,
                             kind, text=text, uid=uid, uname=uname,
                             text_norm=norm, meta=meta, is_bot=is_bot)
        item = {"kind": kind, "ts": ts, "text": text, "uid": uid,
                "uname": uname, "is_bot": is_bot}
        if extra:
            item.update(extra)
        self.hub.publish(item)
        return norm

    def _tail_loop(self, path, handler):
        while not path.exists() and not self._stop.is_set():
            time.sleep(0.5)
        if self._stop.is_set():
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # 启动时先回填最近若干行，避免界面一开始是空的。
            # 回填期间只入库和显示，**不触发决策**——否则会把历史弹幕
            # 当成新消息，一上来就批量回复。
            if self.backfill:
                lines = f.readlines()[-self.backfill:]
                self._backfilling = True
                try:
                    for line in lines:
                        try:
                            handler(line)
                        except Exception:
                            pass
                finally:
                    self._backfilling = False
            f.seek(0, 2)
            while not self._stop.is_set():
                line = f.readline()
                if not line:
                    time.sleep(0.4)
                    continue
                try:
                    handler(line)
                except Exception as exc:
                    self.hub.publish({"kind": "error",
                                      "text": "%s: %s" % (type(exc).__name__, exc),
                                      "ts": int(time.time() * 1000)})

    def on_danmaku_line(self, line):
        # 去掉可能存在的 BOM：日志文件头或追加写入都可能带上 \ufeff
        text_line = line.lstrip("\ufeff").strip()
        m = DANMAKU_RE.match(text_line)
        if not m:
            other = OTHER_RE.match(text_line)
            if other:
                self._on_other_event(other)
            return
        ts = _ts_from_hms(m.group(1))
        uid = int(m.group("uid"))
        uname = m.group("name").strip()
        text = m.group("text").strip()
        meta = {"medal_name": (m.group("medal") or "").strip(),
                "medal_level": int(m.group("lv") or 0)}
        if m.group("rmid"):
            meta["reply_mid"] = int(m.group("rmid"))
            meta["reply_uname"] = (m.group("rname") or "").strip()
        # 三种识别：账号名单 / 房管话术模板 / 这条其实就是我刚才发的那句
        is_bot = 1 if (clean.is_bot_author(uid, uname, self.cfg)
                       or clean.is_bot_reply(text)
                       or self.echo.is_echo(text, ts)) else 0
        norm = self._store_and_publish("danmaku", ts, text, uid, uname, meta,
                                       is_bot)
        self.stats["danmaku"] += 1
        self.last_danmaku_ts = time.time()      # 看门狗用
        if is_bot or norm is None:
            return
        self.store.upsert_user(uid, uname, ts, meta["medal_name"],
                               meta["medal_level"])
        if not self.decide_enabled or self._backfilling:
            return
        event = {"ts": ts, "type": "danmaku", "text": norm, "uid": uid,
                 "uname": uname, "is_bot": 0, "session_id": self.session_id,
                 "meta": meta}
        res = decide_one(self.store, self.llm, self.session_id, event,
                         cfg=self.cfg, state=self.state, index=self.index,
                         room_id=self.room_id)
        self._handle_decision(res, event)

    def _on_other_event(self, m):
        """处理 礼物 / SC / 进入直播间 三类行。

        以前这些行压根没被解析（只认 `[弹幕]`），于是配置里写的 gift/superchat
        触发条件在实时链路上永远不会命中——不是模型不回复，是事件根本没进来。
        礼物和 SC 会走完整决策（可以道谢/@ 回去），进入只记录不触发。
        """
        kind = OTHER_KINDS[m.group("kind")]
        ts = _ts_from_hms(m.group("hms"))
        uid = int(m.group("uid"))
        uname = m.group("uname").strip()
        text = (m.group("text") or "").strip()
        is_bot = 1 if clean.is_bot_author(uid, uname, self.cfg) else 0
        # 不走 clean.normalize_text：那是给弹幕/语音设计的，会把"送出 辣条 x1"
        # 这类结构化文本当噪声筛掉
        self.store.add_event(self.session_id, ts,
                             (ts - self.base_ts) / 1000.0, kind,
                             text=text or uname, uid=uid, uname=uname,
                             text_norm=text or uname, is_bot=is_bot)
        self.hub.publish({"kind": kind, "ts": ts, "text": text,
                          "uid": uid, "uname": uname, "is_bot": is_bot})
        self.stats[kind] = self.stats.get(kind, 0) + 1
        if kind == "enter":
            self.last_danmaku_ts = time.time()    # 也算"采集还活着"
            return
        if is_bot:
            return
        self.last_danmaku_ts = time.time()
        if not self.decide_enabled or self._backfilling:
            return
        event = {"ts": ts, "type": kind, "text": text, "uid": uid,
                 "uname": uname, "is_bot": 0, "session_id": self.session_id}
        res = decide_one(self.store, self.llm, self.session_id, event,
                         cfg=self.cfg, state=self.state, index=self.index,
                         room_id=self.room_id)
        self._handle_decision(res, event)

    def _handle_decision(self, res, event):
        if not res:
            return
        self.stats["triggered"] += 1
        act = res.get("action")
        payload = {"kind": "decision", "action": act,
                   "trigger": res.get("trigger"), "draft": res.get("draft") or "",
                   "reason": res.get("reason") or "",
                   "ts": int(event["ts"])}
        self.hub.publish(payload)
        if act == "shadow":
            if self.auto_send:
                self.sender.put(SendItem(res["draft"],
                                         reply_mid=event.get("uid"),
                                         decision_id=res.get("decision_id"),
                                         trigger=res.get("trigger")))
                self.stats["queued"] += 1
            else:
                self._log("候选(未发送) [%s] %s" % (res.get("trigger"),
                                                   res.get("draft")))

    def on_speech_line(self, line):
        m = SPEECH_RE.match(line.strip())
        if not m:
            return
        text = (m.group("text") or "").strip()
        if (not text or text.startswith("#")
                or text.startswith(SPEECH_CONTROL_PREFIXES)):
            return
        ts = _ts_from_hms(m.group(1))
        norm = self._store_and_publish("speech", ts, text, uname="主播")
        self.stats["speech"] += 1
        self.last_speech_ts = time.time()       # 看门狗用
        if norm is None:
            return
        # 主播语音也要进决策：detect_trigger 对 speech 只认"叫到名字"，
        # 所以量很小，但它承载着 anchor_call 这条通道——不接的话，
        # 语音侧的工具链（add_room_fact / set_topic）只有回放器跑得到。
        if not self.decide_enabled or self._backfilling:
            return
        event = {"ts": ts, "type": "speech", "text": norm, "uid": None,
                 "uname": "主播", "is_bot": 0, "session_id": self.session_id}
        res = decide_one(self.store, self.llm, self.session_id, event,
                         cfg=self.cfg, state=self.state, index=self.index,
                         room_id=self.room_id)
        self._handle_decision(res, event)

    def start(self):
        self.sender.start()
        threads = []
        if self.danmaku_log:
            threads.append(threading.Thread(
                target=self._tail_loop, args=(self.danmaku_log, self.on_danmaku_line),
                daemon=True))
        if self.transcript_log:
            threads.append(threading.Thread(
                target=self._tail_loop, args=(self.transcript_log, self.on_speech_line),
                daemon=True))
        for t in threads:
            t.start()
        threading.Thread(target=self._watchdog_loop, daemon=True).start()
        if self.kb_enabled:
            threading.Thread(target=self._kb_loop, daemon=True).start()
        return self

    def _kb_loop(self):
        """定时做两件知识库维护，都靠 context.summary_interval_s 的节拍。

        1）批量抽取"直播间事实 / 本场话题 / 房间黑话"——**不需要主播叫机器人名字**。
           原来这三个区段只挂在"主播说出小助手"那条实时链路上，
           而主播不会天天对着机器人报备"我明天不播"，所以基本永远是空的。
        2）更新本场滚动摘要（供"本场前面聊过的"区段）。

        两件都用"攒够新素材才跑"的闸门，安静的直播间不产生任何模型调用。
        """
        ccfg = self.cfg.get("context") or {}
        interval = float(ccfg.get("summary_interval_s", 300))
        if interval <= 0:
            return
        min_lines = int(ccfg.get("extract_min_lines", 20))
        max_facts = int(ccfg.get("extract_max_facts", 3))
        daily = int(ccfg.get("extract_daily_limit", 40))
        min_events = int(ccfg.get("summary_min_events", 40))
        keep = int(ccfg.get("summary_keep_chars", 2000))
        people_every = int(ccfg.get("people_refresh_every", 6))
        people_max = int(ccfg.get("people_refresh_max", 3))
        people_stale = float(ccfg.get("people_stale_hours", 12))
        people_once = bool(ccfg.get("people_once_per_session", False))
        people_done_key = "people_done:" + self.session_id
        tick = 0
        from . import extract, kb
        while not self._stop.wait(interval):
            tick += 1
            try:
                n = extract.maybe_extract_room_facts(
                    self.store, self.llm, self.session_id, self.room_id,
                    min_lines=min_lines, max_facts=max_facts,
                    daily_limit=daily, verbose=True)
                if n:
                    self._log("知识库：抽取入库 %d 条" % n)
            except Exception as exc:
                self._log("事实抽取失败: %s: %s" % (type(exc).__name__, exc))
            try:
                text = kb.maybe_update_summary(
                    self.store, self.llm, self.session_id,
                    min_events=min_events, keep_chars=keep)
                if text:
                    self._log("本场摘要已更新（新增摘要 %d 字）" % len(text))
            except Exception as exc:
                self._log("摘要更新失败: %s: %s" % (type(exc).__name__, exc))
            # 增量补向量索引：纯本地计算、不花钱。不补的话"相关的其他场次内容"
            # 永远看不见最近的直播——以前只有手动跑 agent.cli index 才更新。
            if self.index is not None:
                try:
                    added_idx = self.index.build(verbose=False, limit=500)
                    if added_idx:
                        self._log("向量索引补了 %d 条" % added_idx)
                except Exception as exc:
                    self._log("补索引失败: %s: %s" % (type(exc).__name__, exc))
            # 人物卡：默认每场直播只刷一次——第一个知识库节拍时跑一批，
            # 之后用 settings 里的场次标记跳过；服务重启换了场次 id 才会再跑。
            # 旧行为（每 N 拍补几张）仍可用 people_once_per_session=false 打开。
            if people_once:
                if not self.store.get_setting(people_done_key):
                    try:
                        n = kb.build_people(
                            self.store, self.llm, limit=60, min_msg=5,
                            # 本场一次：不按 stale 跳过，否则上一场刚刷过的
                            # 卡会让这一场什么都不更新
                            stale_hours=None,
                            max_people=(people_max or None), verbose=False)
                        self.store.set_setting(people_done_key, "1")
                        self._log("人物卡更新 %d 张（本场一次）" % n)
                    except Exception as exc:
                        self._log("人物卡更新失败: %s: %s"
                                  % (type(exc).__name__, exc))
            elif people_every > 0 and tick % people_every == 0:
                try:
                    n = kb.build_people(self.store, self.llm, limit=60,
                                        min_msg=5, stale_hours=people_stale,
                                        max_people=people_max, verbose=False)
                    if n:
                        self._log("人物卡更新 %d 张" % n)
                except Exception as exc:
                    self._log("人物卡更新失败: %s: %s"
                              % (type(exc).__name__, exc))

    def stop(self):
        self._stop.set()
        self.sender.stop()

    # ---------- 历史回填 ----------
    def recent(self, limit=80):
        rows = self.store.conn.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (self.session_id, limit)).fetchall()
        out = []
        for r in reversed(rows):
            out.append({"kind": r["type"], "ts": r["ts"], "text": r["text"],
                        "uname": r["uname"], "is_bot": r["is_bot"]})
        return out


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>直播实时监控</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif;
     background:#0f1115;color:#e6e6e6;height:100vh;display:flex;flex-direction:column}
header{padding:10px 16px;background:#171a21;border-bottom:1px solid #2a2f3a;
       display:flex;gap:16px;align-items:center}
header b{color:#7cc4ff}
.dot{width:8px;height:8px;border-radius:50%;background:#666;display:inline-block}
.dot.on{background:#3ddc84}
main{flex:1;display:flex;min-height:0}
.col{flex:1;display:flex;flex-direction:column;min-width:0}
.col+.col{border-left:1px solid #2a2f3a}
.col h3{margin:0;padding:8px 14px;font-size:13px;color:#9aa4b2;
        background:#141821;border-bottom:1px solid #2a2f3a;font-weight:500}
.list{flex:1;overflow-y:auto;padding:8px 12px}
.row{padding:4px 0;border-bottom:1px solid #1b1f27;word-break:break-word}
.row .u{color:#7cc4ff}
.row .t{color:#5f6875;font-size:12px;margin-right:6px}
.row.bot{color:#8a8f99;font-style:italic}
.row.speech{color:#ffe08a}
.row.dec{background:#1a2430;border-left:3px solid #3ddc84;padding-left:8px}
.row.dec.rej{background:#2a1f1f;border-left-color:#e06c6c}
.row.noisy{opacity:.55}
footer{padding:10px 14px;background:#171a21;border-top:1px solid #2a2f3a;
       display:flex;gap:10px}
input[type=text]{flex:1;padding:9px 12px;border-radius:6px;border:1px solid #333a46;
                 background:#0f1115;color:#e6e6e6;outline:none}
input[type=text]:focus{border-color:#7cc4ff}
button{padding:9px 18px;border:0;border-radius:6px;background:#2f6feb;color:#fff;
       cursor:pointer}
button:hover{background:#3b7cf7}
#msg{color:#9aa4b2;font-size:12px;align-self:center}
</style></head><body>
<header>
  <span><span class="dot" id="dot"></span> <b id="room">房间</b></span>
  <a href="/" style="color:#e6e6e6;background:#232833;padding:3px 10px;border-radius:6px;text-decoration:none;font-size:13px">实时</a>
  <a href="/history" style="color:#9aa4b2;text-decoration:none;font-size:13px">历史</a>
  <a href="/test" style="color:#9aa4b2;text-decoration:none;font-size:13px">测试</a>
  <a href="/kb" style="color:#9aa4b2;text-decoration:none;font-size:13px">知识库</a>
  <span id="stat">--</span>
  <span id="msg"></span>
</header>
<div id="alert" style="display:none;background:#3a1d1d;color:#ffb4b4;
     padding:8px 16px;border-bottom:1px solid #5a2a2a;font-size:13px"></div>
<main>
  <div class="col"><h3>主播发言（语音转写）</h3><div class="list" id="speech"></div></div>
  <div class="col"><h3>弹幕 / 机器人决策</h3><div class="list" id="danmaku"></div></div>
</main>
<footer>
  <select id="acct" style="padding:9px 10px;border-radius:6px;border:1px solid #333a46;background:#0f1115;color:#e6e6e6">
    <option value="bot">机器人（示例机器人）</option>
    <option value="operator">操作者（我）</option>
  </select>
  <input type="text" id="input" placeholder="手动发弹幕，回车发送..." maxlength="40">
  <button id="send">发送</button>
</footer>
<script>
const ROOM = __ROOM__;
const speechEl = document.getElementById('speech');
const danmakuEl = document.getElementById('danmaku');
const msgEl = document.getElementById('msg');
const dot = document.getElementById('dot');

function fmt(ts){const d=new Date(ts);return d.toTimeString().slice(0,8);}
function add(el, html, cls){
  const div=document.createElement('div');
  div.className='row '+(cls||'');
  div.innerHTML=html;
  el.appendChild(div);
  while(el.children.length>400) el.removeChild(el.firstChild);
  el.scrollTop=el.scrollHeight;
}
function esc(s){return (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

function render(it){
  if(it.kind==='danmaku'){
    const cls = (it.is_bot ? 'bot ' : '') + (it.noise ? 'noisy' : '');
    const tag = it.is_bot ? ' [机器人]' : '';
    add(danmakuEl, '<span class="t">'+fmt(it.ts)+'</span><span class="u">'
        +esc(it.uname)+tag+'</span>: '+esc(it.text), cls);
  } else if(it.kind==='speech'){
    add(speechEl, '<span class="t">'+fmt(it.ts)+'</span>'+esc(it.text), 'speech');
  } else if(it.kind==='decision'){
    const rej = it.action!=='shadow';
    add(danmakuEl, '<span class="t">'+fmt(it.ts)+'</span>🤖 ['+esc(it.trigger)
        +'] '+esc(it.draft)+(it.reason?' — '+esc(it.reason):''), 'dec'+(rej?' rej':''));
  } else if(it.kind==='send'){
    msgEl.textContent = it.ok ? '已发送' : ('发送失败: '+it.message);
  } else if(it.kind==='gift'||it.kind==='superchat'||it.kind==='enter'){
    const icon = it.kind==='gift' ? '🎁' : (it.kind==='superchat' ? '💎' : '➡');
    add(danmakuEl, '<span class="t">'+fmt(it.ts)+'</span>'+icon+' '
        +esc(it.uname)+(it.text?' '+esc(it.text):''), 'dec');
  } else if(it.kind==='error'){
    add(danmakuEl, '<span class="t">'+fmt(it.ts)+'</span>⚠ '+esc(it.text), 'dec rej');
  }
}

fetch('/recent').then(r=>r.json()).then(list=>list.forEach(render));
const es = new EventSource('/stream');
es.onopen = ()=>{dot.className='dot on';};
es.onerror = ()=>{dot.className='dot';
  msgEl.textContent='界面连接断了（后台仍在正常收弹幕），正在自动重连…';};
es.onmessage = e=>{ try{ render(JSON.parse(e.data)); }catch(_){} };

// 运行状态与看门狗告警（采集进程死了要能看出来，而不是"界面没动静"）
const alertEl = document.getElementById('alert');
const statEl = document.getElementById('stat');
async function tick(){
  try{
    const s = await (await fetch('/stats')).json();
    statEl.textContent = '人设 '+(s.persona||'?')+' · 弹幕 '+s.danmaku
      +' · 语音 '+s.speech+' · 触发 '+s.triggered+' · 已发 '+s.sent
      +' · 队列 '+s.queue.sent+'/'+s.queue.expired;
    const a = s.alerts || [];
    if(a.length){
      alertEl.style.display='block';
      alertEl.textContent = '⚠ ' + a.map(x=>x.label+' 已静默 '+x.silent_s
        +' 秒（阈值 '+x.limit_s+'）').join('；') + ' —— ' + a[0].advice;
    } else {
      alertEl.style.display='none';
    }
  }catch(_){}
}
tick(); setInterval(tick, 5000);

async function send(){
  const text = document.getElementById('input').value.trim();
  if(!text) return;
  const account = document.getElementById('acct').value;
  // 不管成功失败都要在信息流里留痕：以前只在页脚闪一行字，
  // 出问题时看起来就像"点了没反应"
  let j = null;
  try {
    const r = await fetch('/send', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({text, account})});
    j = await r.json();
  } catch(e) {
    msgEl.textContent = '发送请求失败：'+e;
    add(danmakuEl, '<span class="t">'+fmt(Date.now())+'</span>⛔ 发送请求失败: '
        +esc(String(e)), 'dec rej');
    return;
  }
  if(j.ok){
    msgEl.textContent = '已入队（'+account+'）';
    add(danmakuEl, '<span class="t">'+fmt(Date.now())+'</span>📤 我('
        +esc(account)+'): '+esc(text), 'dec');
    document.getElementById('input').value='';
  } else {
    msgEl.textContent = '被拦下: '+j.message;
    add(danmakuEl, '<span class="t">'+fmt(Date.now())+'</span>⛔ 发送被拦下: '
        +esc(j.message)+' —— '+esc(text), 'dec rej');
  }
}
document.getElementById('send').onclick = send;
document.getElementById('input').addEventListener('keydown', e=>{
  if(e.key==='Enter') send();
});
</script></body></html>
"""


def make_handler(console):
    jobs = {}

    def _page(name, active):
        css = pages.BASE_CSS
        nav = pages.NAV.format(
            live="on" if active == "live" else "",
            hist="on" if active == "hist" else "",
            test="on" if active == "test" else "",
            kb="on" if active == "kb" else "")
        body = getattr(pages, name)
        return (body.replace("__CSS__", css).replace("__NAV__", nav)
                .replace("__ROOM__", str(console.room_id)))

    def _dt(ms, fmt="%Y-%m-%d %H:%M"):
        return datetime.fromtimestamp(ms / 1000).strftime(fmt)

    def _sessions(store, from_s, to_s, room):
        sql = """SELECT s.id, s.room_id, s.title, s.start_ts,
                 (SELECT MAX(ts) FROM events e WHERE e.session_id=s.id) AS end_ts,
                 (SELECT COUNT(*) FROM events e WHERE e.session_id=s.id
                    AND e.type='danmaku') AS danmaku,
                 (SELECT COUNT(*) FROM events e WHERE e.session_id=s.id
                    AND e.type='speech') AS speech,
                 (SELECT COUNT(DISTINCT uid) FROM events e WHERE e.session_id=s.id
                    AND e.uid IS NOT NULL AND e.is_bot=0) AS users
                 FROM sessions s WHERE 1=1"""
        args = []
        if from_s:
            sql += " AND s.start_ts >= ?"
            args.append(int(from_s * 1000))
        if to_s:
            sql += " AND s.start_ts <= ?"
            args.append(int(to_s * 1000))
        if room:
            sql += " AND s.room_id = ?"
            args.append(int(room))
        sql += " ORDER BY s.start_ts DESC LIMIT 300"
        out = []
        for r in store.conn.execute(sql, args):
            d = dict(r)
            dur = (d["end_ts"] - d["start_ts"]) / 1000 if d["end_ts"] else 0
            d["started"] = _dt(d["start_ts"])
            d["duration"] = ("%d 分" % (dur // 60)) if dur else "-"
            out.append(d)
        return out

    # ---- 知识库页用的只读接口 -------------------------------------------------
    def _memories(store, room, prefix=""):
        """机器人记住了什么（含来源与写入时间，用于人工核对）。"""
        sql = ("SELECT id, uid, room_id, key, value, priority, source, "
               "created_at FROM memories WHERE room_id=?")
        args = [int(room)]
        if prefix:
            sql += " AND key LIKE ?"
            args.append(prefix + "%")
        sql += " ORDER BY priority DESC, id DESC LIMIT 500"
        return [dict(r) for r in store.conn.execute(sql, args)]

    def _kb(store, room):
        """事实卡 + 人物卡。"""
        facts = store.kb_facts(room)
        content = None
        if facts and facts.get("content"):
            try:
                content = json.loads(facts["content"])
            except Exception:
                content = facts["content"]
        meta = None
        if facts:
            meta = {"evidence": facts.get("evidence"),
                    "updated_at": facts.get("updated_at")}
        people = [dict(r) for r in store.conn.execute(
            "SELECT uid, uname, summary, topics, style, quote, msg_count, "
            "updated_at FROM kb_people ORDER BY msg_count DESC LIMIT 200")]
        return {"facts": content, "facts_meta": meta, "people": people}

    def _tool_calls(store, limit):
        """工具调用审计（带 decision_id 就能从决策追到工具）。"""
        return [dict(r) for r in store.conn.execute(
            "SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?", (limit,))]

    def _decisions(store, sid, limit):
        sql = ("SELECT id, session_id, ts, trigger, target_uid, target_uname, "
               "draft, action, reason, latency_ms, model FROM decisions")
        args = []
        if sid:
            sql += " WHERE session_id=?"
            args.append(sid)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in store.conn.execute(sql, args):
            d = dict(r)
            d["clock"] = _dt(d["ts"])
            out.append(d)
        return out

    def _danmaku_for_recording(store, dirname, limit=800):
        """录制目录名形如 123456789_20261014_175750，据此找出同期的弹幕。"""
        try:
            _, stamp = dirname.split("_", 1)
            start = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
        except Exception:
            return []
        dur_ms = 3600 * 1000
        meta = ROOT_AUDIO / dirname / "session.json"
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                dur_ms = int(m.get("file_count", 12) *
                             m.get("segment_seconds", 300) * 1000)
            except Exception:
                pass
        t0 = int(start.timestamp() * 1000)
        rows = []
        for r in store.conn.execute(
                """SELECT * FROM events WHERE type='danmaku' AND ts>=? AND ts<=?
                   ORDER BY ts ASC LIMIT ?""",
                (t0, t0 + dur_ms, limit)):
            d = dict(r)
            d["clock"] = _dt(d["ts"], "%m-%d %H:%M:%S")
            rows.append(d)
        return rows

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html, code=200):
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            path = u.path
            if path == "/":
                body = PAGE.replace("__ROOM__", str(console.room_id)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path in ("/history", "/test"):
                html = _page("HISTORY" if path == "/history" else "TEST",
                             "hist" if path == "/history" else "test")
                self._send_html(html)
            elif path == "/kb":
                self._send_html(_page("KB", "kb"))
            elif path == "/api/memories":
                q = parse_qs(u.query)
                room = q.get("room", [str(console.room_id)])[0]
                prefix = q.get("prefix", [""])[0]
                self._json(_memories(console.store, room, prefix))
            elif path == "/api/kb":
                q = parse_qs(u.query)
                room = int(q.get("room", [str(console.room_id)])[0]
                           or console.room_id)
                self._json(_kb(console.store, room))
            elif path == "/api/tool_calls":
                q = parse_qs(u.query)
                limit = min(int(q.get("limit", ["80"])[0]), 500)
                self._json(_tool_calls(console.store, limit))
            elif path == "/api/decisions":
                q = parse_qs(u.query)
                sid = q.get("session", [""])[0] or None
                limit = min(int(q.get("limit", ["60"])[0]), 500)
                self._json(_decisions(console.store, sid, limit))
            elif path == "/api/sessions":
                q = parse_qs(u.query)
                f = q.get("from", [""])[0]
                t = q.get("to", [""])[0]
                room = q.get("room", [""])[0]
                fs = datetime.strptime(f, "%Y-%m-%d").timestamp() if f else None
                ts = (datetime.strptime(t, "%Y-%m-%d").timestamp() + 86399
                      if t else None)
                self._json(_sessions(console.store, fs, ts, room))
            elif path == "/api/events":
                q = parse_qs(u.query)
                sid = q.get("session", [""])[0]
                limit = min(int(q.get("limit", ["300"])[0]), 2000)
                typ = q.get("type", [""])[0]
                kw = q.get("q", [""])[0]
                sql = "SELECT * FROM events WHERE session_id=?"
                args = [sid]
                if typ:
                    sql += " AND type=?"
                    args.append(typ)
                if kw:
                    sql += " AND (text LIKE ? OR uname LIKE ?)"
                    args += ["%" + kw + "%", "%" + kw + "%"]
                sql += " ORDER BY ts ASC LIMIT ?"
                args.append(limit)
                rows = []
                for r in console.store.conn.execute(sql, args):
                    d = dict(r)
                    d["clock"] = _dt(d["ts"], "%m-%d %H:%M:%S")
                    rows.append(d)
                self._json(rows)
            elif path == "/api/recordings":
                self._json(list_recordings())
            elif path == "/api/transcribe/status":
                q = parse_qs(u.query)
                d = q.get("dir", [""])[0]
                j = jobs.get(d)
                self._json(j.status() if j else {"name": d, "running": False,
                                                 "exit_code": None, "log": [],
                                                 "kept": 0, "elapsed": 0})
            elif path == "/api/transcribe/result":
                q = parse_qs(u.query)
                d = q.get("dir", [""])[0]
                f = ROOT_AUDIO / d / "transcript.txt"
                rows = []
                if f.exists():
                    for line in f.read_text(encoding="utf-8",
                                            errors="replace").splitlines():
                        m = re.match(r"^\[(\+\d+:\d+)\]\s+(.*)$", line.strip())
                        if m:
                            rows.append({"clock": m.group(1), "text": m.group(2)})
                self._json(rows)
            elif path == "/api/danmaku":
                q = parse_qs(u.query)
                d = q.get("dir", [""])[0]
                self._json(_danmaku_for_recording(console.store, d))
            elif path == "/recent":
                self._json(console.recent())
            elif path == "/stats":
                st = dict(console.stats)
                st["queue"] = console.sender.stats
                st["session"] = console.session_id
                st["persona"] = console.store.get_setting("persona", "normal")
                now = time.time()
                st["alerts"] = console.alerts
                st["danmaku_silent_s"] = (int(now - console.last_danmaku_ts)
                                          if console.last_danmaku_ts else None)
                st["speech_silent_s"] = (int(now - console.last_speech_ts)
                                         if console.last_speech_ts else None)
                self._json(st)
            elif path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = console.hub.subscribe()
                try:
                    while True:
                        try:
                            item = q.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        data = json.dumps(item, ensure_ascii=False).encode("utf-8")
                        self.wfile.write(b"data: " + data + b"\n\n")
                        self.wfile.flush()
                except OSError:
                    # 浏览器关页/断网时 Windows 抛的是 ConnectionAbortedError
                    # （10053），它不在 ConnectionReset/BrokenPipe 之下，只捕
                    # 那两个会让 traceback 一直刷 web.err。
                    pass
                finally:
                    console.hub.unsubscribe(q)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._json({"ok": False, "message": "请求格式错误"}, 400)
                return
            if path == "/send":
                res = console.manual_send(payload.get("text", ""),
                                          int(payload.get("reply_mid") or 0),
                                          payload.get("account") or "bot")
                self._json(res)
            elif path == "/api/transcribe":
                d = payload.get("dir", "")
                if not d:
                    self._json({"ok": False, "message": "缺少 dir"}, 400)
                    return
                j = jobs.get(d)
                if j is None or not (j.proc and j.proc.poll() is None):
                    j = TranscribeJob(
                        d, console.room_id,
                        extra_args=["--separate", "--vad", "--singing-filter",
                                    "--chunk", "8"])
                    jobs[d] = j.start()
                self._json({"ok": True, "dir": d})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


class _Server(ThreadingHTTPServer):
    # 默认 allow_reuse_address=True 在 Windows 上允许两个进程绑同一端口，
    # 请求会被随机分发，表现为"时好时坏"。这里禁掉，端口占用时直接报错。
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        """浏览器断开 keep-alive 连接是常态，不该打整段 traceback。

        这类异常发生在读请求行阶段（还没进 do_GET），只能在 socketserver
        的兜底回调里吞掉；其它异常照常打印，否则真问题会被一起藏起来。
        """
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            return
        super().handle_error(request, client_address)


def serve(console, port=8765, host="127.0.0.1"):
    try:
        httpd = _Server((host, port), make_handler(console))
    except OSError as exc:
        raise SystemExit("端口 %d 已被占用（%s）。换一个端口或用 --port 指定。"
                         % (port, exc))
    print("界面已启动: http://%s:%d" % (host, port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        console.stop()
