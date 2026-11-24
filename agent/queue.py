"""发送队列：按序发送、限速、带 TTL。

规则（来自需求）:
  1. 两条弹幕之间至少间隔 3 秒
  2. 每分钟最多发送 8 条
  3. 先进先出，按顺序发送
  4. 每条消息在队列里最多存活 1 分钟，超时直接丢弃（不再发送）

注意 TTL 是"排队等待"的耐心限度：如果积压超过 1 分钟，
说明这条消息已经错过了合适的时机，发出去也不合语境，所以丢掉而不是补发。
"""

import queue
import threading
import time


class SendItem:
    __slots__ = ("text", "reply_mid", "decision_id", "enqueued_at", "trigger",
                 "ttl_s", "account")

    def __init__(self, text, reply_mid=0, decision_id=None, trigger=None,
                 ttl_s=None, account="bot"):
        self.text = text
        self.reply_mid = reply_mid
        self.decision_id = decision_id
        self.trigger = trigger
        self.ttl_s = ttl_s          # 覆盖队列默认 TTL（手动发送可放宽）
        self.account = account      # 用哪个身份发：bot / operator
        self.enqueued_at = time.time()


class SendQueue:
    """带限速与 TTL 的发送队列。send_fn(item) 返回结果字典。"""

    def __init__(self, send_fn, min_interval_s=3.0, per_minute=8, ttl_s=60.0,
                 log=None):
        self.send_fn = send_fn
        self.min_interval_s = min_interval_s
        self.per_minute = per_minute
        self.ttl_s = ttl_s
        self.log = log or (lambda m: None)
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self.sent_ts = []
        self.stats = {"sent": 0, "expired": 0, "failed": 0}
        # 回调由外部注入：on_sent(item, result) / on_expire(item)
        self.on_sent = None
        self.on_expire = None

    def put(self, item):
        self._q.put(item)
        self.log("入队: %s（队列长度 %d）" % (item.text[:20], self._q.qsize()))

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self, drain_timeout=5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=drain_timeout)

    def _prune(self, now):
        self.sent_ts = [t for t in self.sent_ts if now - t < 60]

    def _wait_until_allowed(self, item):
        """等到可以发送为止。等待期间若超过 TTL，返回 False 表示该丢弃。"""
        ttl = item.ttl_s if item.ttl_s else self.ttl_s
        while not self._stop.is_set():
            now = time.time()
            if now - item.enqueued_at > ttl:
                return False
            self._prune(now)
            wait = 0.0
            if self.sent_ts:
                wait = max(wait, self.sent_ts[-1] + self.min_interval_s - now)
            if len(self.sent_ts) >= self.per_minute:
                wait = max(wait, self.sent_ts[0] + 60 - now)
            if wait <= 0:
                return True
            # 最多等到 TTL 到期那一刻，避免无谓的长时间阻塞
            self._stop.wait(min(wait, 0.5))
        return False

    def _loop(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._wait_until_allowed(item):
                self.stats["expired"] += 1
                ttl = item.ttl_s if item.ttl_s else self.ttl_s
                self.log("丢弃(超时 %ds): %s" % (int(ttl), item.text[:20]))
                if self.on_expire:
                    self.on_expire(item)
                continue
            try:
                out = self.send_fn(item)
            except Exception as exc:
                self.stats["failed"] += 1
                self.log("发送异常: %s" % exc)
                continue
            if out.get("code") == 0:
                self.stats["sent"] += 1
                self.sent_ts.append(time.time())
                self.log("已发送: %s" % item.text[:24])
            else:
                self.stats["failed"] += 1
                self.log("发送失败 code=%s %s" % (out.get("code"),
                                                 out.get("message")))
            if self.on_sent:
                self.on_sent(item, out)
