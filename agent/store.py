"""情报库：SQLite schema、落库与查询。

设计见 docs/agent_design.md 第 4 章。所有数据源（弹幕/语音/礼物/事件）
统一落到 events 表，用 session_id + ts 串成一条时间轴。
"""

import json
import hashlib
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id              TEXT PRIMARY KEY,
  room_id         INTEGER,
  title           TEXT,
  start_ts        INTEGER,
  end_ts          INTEGER,
  peak_online     INTEGER,
  danmaku_count   INTEGER DEFAULT 0,
  speech_seconds  REAL DEFAULT 0,
  note            TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT    NOT NULL,
  ts         INTEGER NOT NULL,
  offset_s   REAL    NOT NULL,
  type       TEXT    NOT NULL,
  uid        INTEGER,
  uname      TEXT,
  text       TEXT,
  text_norm  TEXT,
  is_bot     INTEGER DEFAULT 0,
  is_noise   INTEGER DEFAULT 0,
  meta       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_session_ts ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_ev_type       ON events(type);
CREATE INDEX IF NOT EXISTS idx_ev_uid        ON events(uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ev_dedup ON events(session_id, type, ts, IFNULL(uid,-1), IFNULL(text_norm,''));

CREATE TABLE IF NOT EXISTS users (
  uid           INTEGER PRIMARY KEY,
  uname         TEXT,
  first_seen    INTEGER,
  last_seen     INTEGER,
  msg_count     INTEGER DEFAULT 0,
  medal_name    TEXT,
  medal_level   INTEGER DEFAULT 0,
  guard_level   INTEGER DEFAULT 0,
  tags          TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  ts         INTEGER,
  trigger    TEXT,
  target_uid INTEGER,
  target_uname TEXT,
  context    TEXT,
  draft      TEXT,
  action     TEXT,
  reason     TEXT,
  latency_ms INTEGER,
  model      TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
  USING fts5(text_norm, content='events', content_rowid='id', tokenize='trigram');

-- 向量索引：语义检索用（原来的 FTS 对口语无效，见 docs/agent_reply_optimization.md）
CREATE TABLE IF NOT EXISTS vectors (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  kind   TEXT NOT NULL,      -- speech / danmaku
  ref_id INTEGER NOT NULL,   -- 对应 events.id
  text   TEXT,
  vec    BLOB NOT NULL       -- float32 向量
);
CREATE INDEX IF NOT EXISTS idx_vec_kind_ref ON vectors(kind, ref_id);

-- 知识库：事实层（主播的固定信息）
CREATE TABLE IF NOT EXISTS kb_facts (
  room_id    INTEGER PRIMARY KEY,
  content    TEXT,
  evidence   TEXT,
  updated_at TEXT
);

-- 知识库：人物层（每个观众的印象）
CREATE TABLE IF NOT EXISTS kb_people (
  uid        INTEGER PRIMARY KEY,
  uname      TEXT,
  summary    TEXT,
  topics     TEXT,
  style      TEXT,
  quote      TEXT,
  msg_count  INTEGER DEFAULT 0,
  updated_at TEXT
);

-- 本场滚动摘要：解决"只能记住最后 3 分钟"
CREATE TABLE IF NOT EXISTS session_summaries (
  session_id TEXT PRIMARY KEY,
  content    TEXT,
  through_ts INTEGER,
  updated_at TEXT
);

-- 记忆：工具链写入的内容，带来源与优先级
CREATE TABLE IF NOT EXISTS memories (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  uid        INTEGER,          -- 属于谁；房间级为 NULL
  room_id    INTEGER,
  key        TEXT NOT NULL,    -- alias / note:xx / pref:xx / slang:xx / fact:xx / ignore
  value      TEXT,
  priority   INTEGER DEFAULT 1,
  source     TEXT,
  source_uid INTEGER,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_uid ON memories(uid, key);
CREATE INDEX IF NOT EXISTS idx_mem_room ON memories(room_id, key);

-- 工具调用审计
CREATE TABLE IF NOT EXISTS tool_calls (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id INTEGER,
  tool        TEXT,
  args        TEXT,
  status      TEXT,
  reason      TEXT,
  created_at  TEXT
);

-- 全局设置（当前人设等）
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, text_norm) VALUES (new.id, new.text_norm);
END;
"""


class Store:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：发送队列在独立线程里回调写库，
        # 跨线程访问需要放开限制，并由 _lock 串行化写入。
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """旧库补列。"""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(events)")]
        if "is_bot" not in cols:
            self.conn.execute(
                "ALTER TABLE events ADD COLUMN is_bot INTEGER DEFAULT 0")
        if "is_noise" not in cols:
            self.conn.execute(
                "ALTER TABLE events ADD COLUMN is_noise INTEGER DEFAULT 0")

    def close(self):
        self.conn.close()

    # ---------- 场次 ----------
    def ensure_session(self, session_id, room_id, title=None, start_ts=None):
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions(id, room_id, title, start_ts) "
                "VALUES (?,?,?,?)",
                (session_id, room_id, title, start_ts),
            )
            self.conn.commit()

    def sessions(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM sessions ORDER BY start_ts DESC")]

    # ---------- 事件 ----------
    def add_event(self, session_id, ts, offset_s, type_, text, uid=None,
                  uname=None, text_norm=None, meta=None, is_bot=0,
                  is_noise=0):
        """插入事件；同一 (session,type,ts,uid,text) 重复插入会被唯一索引挡掉。"""
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO events
                   (session_id, ts, offset_s, type, uid, uname, text, text_norm,
                    is_bot, meta, is_noise)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, int(ts), float(offset_s), type_, uid, uname, text,
                 text_norm if text_norm is not None else text,
                 int(is_bot or 0),
                 json.dumps(meta, ensure_ascii=False) if meta else None,
                 int(is_noise or 0)),
            )
            self.conn.commit()
            return cur.rowcount

    def recent_events(self, session_id, before_ts=None, window_s=180, types=None,
                      exclude_uid=None, include_bot=False, include_noise=False):
        """取某时刻之前 window_s 秒内的事件，按时间正序。"""
        before_ts = before_ts or int(time.time() * 1000)
        sql = ("SELECT * FROM events WHERE session_id=? AND ts>? AND ts<=?")
        args = [session_id, int(before_ts - window_s * 1000), int(before_ts)]
        if not include_bot:
            sql += " AND is_bot=0"
        if not include_noise:
            # 纯表情/单字这类噪声照常入库和显示，但不进提示词——
            # 近场窗口只有 25 行，被表情挤掉就没上下文了
            sql += " AND is_noise=0"
        if types:
            sql += " AND type IN (%s)" % ",".join("?" * len(types))
            args += list(types)
        if exclude_uid is not None:
            sql += " AND (uid IS NULL OR uid<>?)"
            args.append(exclude_uid)
        sql += " ORDER BY ts ASC"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def search_text(self, query, limit=10, session_id=None):
        """全文检索（FTS5 trigram）。"""
        if len(query.strip()) < 3:
            like = f"%{query}%"
            sql = "SELECT * FROM events WHERE text_norm LIKE ?"
            args = [like]
            if session_id:
                sql += " AND session_id=?"
                args.append(session_id)
            sql += " ORDER BY ts DESC LIMIT ?"
            args.append(limit)
            return [dict(r) for r in self.conn.execute(sql, args)]
        sql = ("SELECT e.* FROM events_fts f JOIN events e ON e.id=f.rowid "
               "WHERE events_fts MATCH ?")
        args = [f'"{query}"']
        if session_id:
            sql += " AND e.session_id=?"
            args.append(session_id)
        sql += " ORDER BY e.ts DESC LIMIT ?"
        args.append(limit)
        try:
            return [dict(r) for r in self.conn.execute(sql, args)]
        except sqlite3.OperationalError:
            return []

    # ---------- 用户画像 ----------
    def upsert_user(self, uid, uname, ts, medal_name=None, medal_level=0, guard_level=0):
        if not uid:
            return
        with self._lock:
            row = self.conn.execute(
                "SELECT uid FROM users WHERE uid=?", (uid,)).fetchone()
            if row:
                self.conn.execute(
                    """UPDATE users SET uname=?, last_seen=?, msg_count=msg_count+1,
                       medal_name=COALESCE(?,medal_name),
                       medal_level=MAX(medal_level,?), guard_level=MAX(guard_level,?)
                       WHERE uid=?""",
                    (uname, int(ts), medal_name, medal_level or 0,
                     guard_level or 0, uid))
            else:
                self.conn.execute(
                    """INSERT INTO users(uid,uname,first_seen,last_seen,msg_count,
                       medal_name,medal_level,guard_level)
                       VALUES (?,?,?,?,1,?,?,?)""",
                    (uid, uname, int(ts), int(ts), medal_name,
                     medal_level or 0, guard_level or 0))
            self.conn.commit()

    def user_profile(self, uid):
        r = self.conn.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        return dict(r) if r else None

    def top_users(self, limit=10):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM users ORDER BY msg_count DESC LIMIT ?", (limit,))]

    # ---------- 决策审计 ----------
    def add_decision(self, session_id, ts, trigger, draft, action, reason=None,
                     target_uid=None, target_uname=None, context=None,
                     latency_ms=None, model=None):
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO decisions(session_id,ts,trigger,target_uid,
                   target_uname,context,draft,action,reason,latency_ms,model)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, int(ts), trigger, target_uid, target_uname,
                 json.dumps(context, ensure_ascii=False) if context else None,
                 draft, action, reason, latency_ms, model))
            self.conn.commit()
            return cur.lastrowid

    def decisions(self, session_id=None, limit=50):
        sql = "SELECT * FROM decisions"
        args = []
        if session_id:
            sql += " WHERE session_id=?"
            args.append(session_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def update_decision(self, decision_id, action=None, reason=None, draft=None):
        sets, args = [], []
        if action is not None:
            sets.append("action=?")
            args.append(action)
        if reason is not None:
            sets.append("reason=?")
            args.append(reason)
        if draft is not None:
            sets.append("draft=?")
            args.append(draft)
        if not sets:
            return
        args.append(decision_id)
        with self._lock:
            self.conn.execute(
                "UPDATE decisions SET %s WHERE id=?" % ",".join(sets), args)
            self.conn.commit()

    # ---------- 统计 ----------
    def stats(self, session_id):
        c = self.conn.execute
        out = {"session_id": session_id}
        for t in ("danmaku", "speech", "gift", "superchat", "enter"):
            out[t] = c("SELECT COUNT(*) FROM events WHERE session_id=? AND type=?",
                       (session_id, t)).fetchone()[0]
        out["users"] = c("""SELECT COUNT(DISTINCT uid) FROM events
                            WHERE session_id=? AND uid IS NOT NULL""",
                         (session_id,)).fetchone()[0]
        span = c("SELECT MIN(ts), MAX(ts) FROM events WHERE session_id=?",
                 (session_id,)).fetchone()
        out["span_s"] = round((span[1] - span[0]) / 1000, 1) if span[0] else 0
        return out

    # ---------- 知识库 ----------
    def set_kb_facts(self, room_id, content, evidence=""):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO kb_facts(room_id, content, evidence, "
                "updated_at) VALUES (?,?,?,?)",
                (room_id, content, evidence,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def kb_facts(self, room_id):
        r = self.conn.execute(
            "SELECT content, updated_at FROM kb_facts WHERE room_id=?",
            (room_id,)).fetchone()
        return dict(r) if r else None

    def set_kb_person(self, uid, uname, summary, topics="", style="",
                      quote="", msg_count=0):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO kb_people(uid, uname, summary, topics, "
                "style, quote, msg_count, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, uname, summary, topics, style, quote, msg_count,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def kb_person(self, uid):
        if not uid:
            return None
        r = self.conn.execute(
            "SELECT * FROM kb_people WHERE uid=?", (uid,)).fetchone()
        return dict(r) if r else None

    def top_speakers(self, limit=20, min_msg=5):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM users WHERE msg_count>=? ORDER BY msg_count DESC "
            "LIMIT ?", (min_msg, limit))]

    def user_messages(self, uid, limit=60):
        return [dict(r) for r in self.conn.execute(
            "SELECT text, ts FROM events WHERE uid=? AND type='danmaku' "
            "AND text IS NOT NULL ORDER BY ts DESC LIMIT ?", (uid, limit))]

    def set_session_summary(self, session_id, content, through_ts):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO session_summaries(session_id, content, "
                "through_ts, updated_at) VALUES (?,?,?,?)",
                (session_id, content, int(through_ts),
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def session_summary(self, session_id):
        r = self.conn.execute(
            "SELECT content, through_ts FROM session_summaries WHERE session_id=?",
            (session_id,)).fetchone()
        return dict(r) if r else None

    def session_room(self, session_id):
        """会话属于哪个房间。调用方忘了传 room_id 时的兜底。"""
        r = self.conn.execute("SELECT room_id FROM sessions WHERE id=?",
                              (session_id,)).fetchone()
        return int(r[0]) if r and r[0] else None

    # ---------- 记忆与工具 ----------
    def set_memory(self, uid, room_id, key, value, priority=1,
                   source="", source_uid=None):
        """写入记忆。低优先级不能覆盖高优先级。"""
        with self._lock:
            cur = self.conn.execute(
                "SELECT priority FROM memories WHERE IFNULL(uid,-1)=IFNULL(?,-1) "
                "AND IFNULL(room_id,-1)=IFNULL(?,-1) AND key=?",
                (uid, room_id, key)).fetchone()
            if cur and cur[0] > priority:
                return False
            self.conn.execute(
                "DELETE FROM memories WHERE IFNULL(uid,-1)=IFNULL(?,-1) "
                "AND IFNULL(room_id,-1)=IFNULL(?,-1) AND key=?",
                (uid, room_id, key))
            self.conn.execute(
                "INSERT INTO memories(uid, room_id, key, value, priority, "
                "source, source_uid, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, room_id, key, value, priority, source, source_uid,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()
            return True

    def get_memory(self, uid, room_id, key):
        r = self.conn.execute(
            "SELECT value FROM memories WHERE IFNULL(uid,-1)=IFNULL(?,-1) "
            "AND IFNULL(room_id,-1)=IFNULL(?,-1) AND key=?",
            (uid, room_id, key)).fetchone()
        return r[0] if r else None

    def memories_for(self, uid, room_id):
        """取"该注入给这个人的记忆"。

        只包含两类，绝不能多：
          1) 他本人的记录（uid 匹配）
          2) 房间级记录（uid 为 NULL 的 slang/fact/topic）

        原实现写成 `uid=? OR room_id=?`，会把**同一房间其他人的私人记忆**
        一起捞出来（比如 A 的别名出现在 B 的提示词里），既是隐私问题，
        也会让模型拿到互相矛盾的称呼。

        uid 传 None 也合法：SQL 里 `uid = NULL` 永不成立，于是只返回房间级
        记录——主播语音触发的那一轮需要的就是这个（它没有"自己"）。

        排序用 `priority DESC, id DESC`：id 是自增的插入序，
        比 created_at（只到秒）可靠，同一秒写入的多条不会乱序。
        """
        return [dict(r) for r in self.conn.execute(
            "SELECT id, key, value, created_at FROM memories WHERE uid=? "
            "OR (room_id=? AND uid IS NULL) "
            "ORDER BY priority DESC, id DESC", (uid, room_id))]

    def set_note(self, uid, room_id, note, priority=2, source="",
                 source_uid=None):
        """写一条"关于这个人"的备注。

        键取内容前 12 字会让两条不同的备注互相覆盖（"我在考研" vs
        "我在考研复习"），模型事后也没法猜这个内部键去删除。
        所以键用内容指纹：同样的内容重复说是更新，不同内容是新增。
        """
        note = (note or "").strip()
        if not note or uid is None:
            return False
        key = "note:" + hashlib.md5(note.encode("utf-8")).hexdigest()[:10]
        return self.set_memory(uid, room_id, key, note, priority, source,
                               source_uid)

    def forget_matching(self, uid, room_id, text):
        """按"人话"删除记忆：别名、备注、偏好都按内容/键模糊匹配。

        模型不该被迫猜内部键（`note:<指纹>`），它说"忘掉考研的事"，
        这里就按内容把相关记录删掉。返回删除条数。
        """
        text = (text or "").strip()
        if not text:
            return 0
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM memories WHERE IFNULL(uid,-1)=IFNULL(?,-1) "
                "AND IFNULL(room_id,-1)=IFNULL(?,-1) "
                "AND (value LIKE ? OR key LIKE ?)",
                (uid, room_id, "%" + text + "%", "%" + text + "%"))
            self.conn.commit()
            return cur.rowcount

    def count_events_since(self, session_id, ts):
        """本场在 ts（毫秒）之后新增了多少条事件。摘要循环用它判断值不值得跑。"""
        r = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id=? AND ts>?",
            (session_id, int(ts or 0))).fetchone()
        return r[0] if r else 0

    def delete_memory(self, uid, room_id, key):
        with self._lock:
            self.conn.execute(
                "DELETE FROM memories WHERE IFNULL(uid,-1)=IFNULL(?,-1) "
                "AND IFNULL(room_id,-1)=IFNULL(?,-1) AND key LIKE ?",
                (uid, room_id, key + "%"))
            self.conn.commit()

    def is_ignored(self, uid, room_id):
        if not uid:
            return False
        return self.get_memory(uid, room_id, "ignore") is not None

    def writes_today(self, uid, room_id=None):
        """今天由 uid 写入了几条记忆。

        用 IFNULL 比较而不是 `=`：主播语音写入的 source_uid 是 NULL，
        `source_uid = NULL` 在 SQL 里永不成立，额度检查会形同虚设
        （复核时实测 writes_today(None) 恒为 0）。
        room_id 给了就只数这个房间的，用于房间级（主播来源）的额度。
        """
        sql = ("SELECT COUNT(*) FROM memories WHERE "
               "IFNULL(source_uid,-1)=IFNULL(?,-1) AND created_at LIKE ?")
        args = [uid, time.strftime("%Y-%m-%d") + "%"]
        if room_id is not None:
            sql += " AND room_id=?"
            args.append(room_id)
        r = self.conn.execute(sql, args).fetchone()
        return r[0] if r else 0

    def add_tool_call(self, decision_id, tool, args, status, reason=""):
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO tool_calls(decision_id, tool, args, status, "
                "reason, created_at) VALUES (?,?,?,?,?,?)",
                (decision_id, tool, args, status, reason,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()
            return cur.lastrowid

    def link_tool_calls(self, ids, decision_id):
        """把本轮的工具调用挂到决策上。

        工具在 add_decision 之前执行（要先知道执行结果才好让模型说话），
        所以当时拿不到 decision_id，只能事后回填。不回填的话审计表里
        decision_id 恒为 NULL，出了问题没法从决策追到工具。
        """
        ids = [i for i in (ids or []) if i]
        if not ids or not decision_id:
            return 0
        with self._lock:
            cur = self.conn.executemany(
                "UPDATE tool_calls SET decision_id=? WHERE id=?",
                [(decision_id, i) for i in ids])
            self.conn.commit()
            return cur.rowcount

    def recent_tool_calls(self, limit=30):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?", (limit,))]

    def set_setting(self, key, value):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO settings(key, value, updated_at) "
                "VALUES (?,?,?)",
                (key, value, time.strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def get_setting(self, key, default=None):
        r = self.conn.execute("SELECT value FROM settings WHERE key=?",
                              (key,)).fetchone()
        return r[0] if r else default

    def set_session_topic(self, session_id, topic, room_id=None, source=""):
        """记录本场当前在聊什么。

        必须带上 room_id：读侧走 memories_for 的 `(room_id=? AND uid IS NULL)`
        这一桶，写成 room_id=NULL 的记录谁也取不回来（复核时实测）。
        键里带 session_id，所以只会取到"本场"的话题，不会把往期的
        「上次聊的」当成「现在聊的」注进提示词。
        source 记下它从哪来（抽取窗口 / 触发原文），和 fact 一样可追溯。
        """
        if session_id and topic:
            self.set_memory(None, room_id, "topic:" + session_id, topic, 3,
                            source, None)

    def session_topic(self, session_id, room_id):
        """取本场话题；没有就返回 None。"""
        if not session_id:
            return None
        return self.get_memory(None, room_id, "topic:" + session_id)

    def purge(self, session_id=None, uid=None):
        """删除历史记录（仅操作者可用，调用方负责鉴权）。"""
        with self._lock:
            n = 0
            if session_id:
                n += self.conn.execute("DELETE FROM events WHERE session_id=?",
                                       (session_id,)).rowcount
                self.conn.execute("DELETE FROM sessions WHERE id=?",
                                  (session_id,))
                self.conn.execute(
                    "DELETE FROM vectors WHERE ref_id NOT IN "
                    "(SELECT id FROM events)")
            if uid:
                n += self.conn.execute(
                    "DELETE FROM events WHERE uid=? AND type='danmaku'",
                    (uid,)).rowcount
            self.conn.commit()
            return n
