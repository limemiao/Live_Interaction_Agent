"""执行层：把决策真正发出去。

设计上刻意做成**显式调用**——必须指定要发哪条 decision id，
不提供"自动发送"入口。每次发送的结果都回写到 decisions 表。
"""

import json
import time
from pathlib import Path

import requests

SEND_URL = "https://api.live.bilibili.com/msg/send"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ERROR_CODES = {
    0: "成功",
    -101: "未登录",
    -111: "csrf 校验失败",
    -400: "参数错误",
    -403: "权限不足（可能被禁言）",
    10030: "发送频率过快或被限制",
    1003212: "超出限制长度",
}


def load_cookies(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("cookies") or {}, data


def send_danmaku(cookies, room_id, text, reply_mid=0, timeout=20):
    """发送一条弹幕。reply_mid 非 0 时表示回复某人。"""
    csrf = cookies.get("bili_jct")
    if not cookies.get("SESSDATA") or not csrf:
        return {"code": -1, "message": "Cookie 缺少 SESSDATA 或 bili_jct"}
    form = {
        "bubble": "0", "msg": text, "color": "16777215", "mode": "1",
        "fontsize": "25", "rnd": str(int(time.time())), "roomid": str(room_id),
        "csrf": csrf, "csrf_token": csrf,
    }
    if reply_mid:
        form["reply_mid"] = str(reply_mid)
    r = requests.post(SEND_URL, data=form, cookies=cookies, timeout=timeout,
                      headers={"User-Agent": UA,
                               "Referer": "https://live.bilibili.com/%d" % room_id,
                               "Origin": "https://live.bilibili.com"})
    try:
        res = r.json()
    except Exception:
        return {"code": -1, "message": "响应非 JSON: %s" % r.text[:120]}
    code = res.get("code")
    out = {"code": code, "message": ERROR_CODES.get(code, res.get("message") or "")}
    extra_raw = ((res.get("data") or {}).get("mode_info") or {}).get("extra")
    if extra_raw:
        extra = json.loads(extra_raw)
        out["echo"] = extra.get("content")
        out["reply_mid"] = extra.get("reply_mid")
        out["reply_uname"] = extra.get("reply_uname")
        out["reply_type_enum"] = extra.get("reply_type_enum")
    return out
