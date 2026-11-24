#!/usr/bin/env python3
"""用登录态在 B 站直播间发送弹幕。

接口:
    POST https://api.live.bilibili.com/msg/send     (form-urlencoded)
    必需: roomid / msg / csrf(=bili_jct) / rnd / mode / color / fontsize
    Cookie: SESSDATA(登录态) + bili_jct(csrf token) + DedeUserID

用法:
    python -m integrations.bili_send_danmaku --room-id 123456789 --text "测试弹幕" --dry-run
    python -m integrations.bili_send_danmaku --room-id 123456789 --text "测试弹幕"

注意:
    弹幕是公开可见的，发出去就撤不回来；先 --dry-run 看清楚再真发。
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SEND_URL = "https://api.live.bilibili.com/msg/send"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 常见返回码
CODES = {
    0: "发送成功",
    -101: "账号未登录",
    -111: "csrf 校验失败",
    -400: "请求参数错误",
    -403: "权限不足（可能被禁言或该房间限制）",
    10030: "发送频率过快 / 被限制",
    1003212: "弹幕内容过长",
}


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="B 站直播间发送弹幕")
    ap.add_argument("--room-id", type=int, required=True)
    ap.add_argument("--text", required=True, help="弹幕内容")
    ap.add_argument("--cookie-file", default=str(here / "bili_cookies.json"))
    ap.add_argument("--color", type=int, default=16777215, help="十进制颜色，默认白色")
    ap.add_argument("--mode", type=int, default=1, help="1=滚动 4=底部 5=顶部")
    ap.add_argument("--fontsize", type=int, default=25)
    ap.add_argument("--dry-run", action="store_true", help="只打印请求内容，不真的发送")
    ap.add_argument("--reply-mid", type=int, default=0,
                    help="回复某人：填对方 uid。文本里再写 @昵称 只是为了好看，"
                         "真正建立回复关系靠这个参数")
    args = ap.parse_args()

    cookie_path = Path(args.cookie_file)
    if not cookie_path.exists():
        print(f"Cookie 文件不存在: {cookie_path}（先跑 bili_login.py）")
        return 1
    payload = json.loads(cookie_path.read_text(encoding="utf-8"))
    cookies = payload.get("cookies") or {}
    csrf = cookies.get("bili_jct")
    if not cookies.get("SESSDATA") or not csrf:
        print("Cookie 里缺少 SESSDATA 或 bili_jct，请重新登录。")
        return 1

    text = args.text.strip()
    if not text:
        print("弹幕内容不能为空。")
        return 1
    if len(text) > 20:
        print(f"提示: 内容 {len(text)} 字，超过普通用户 20 字上限时会被接口拒绝。")

    form = {
        "bubble": "0",
        "msg": text,
        "color": str(args.color),
        "mode": str(args.mode),
        "fontsize": str(args.fontsize),
        "rnd": str(int(time.time())),
        "roomid": str(args.room_id),
        "csrf": csrf,
        "csrf_token": csrf,
    }
    if args.reply_mid:
        form["reply_mid"] = str(args.reply_mid)
    headers = {
        "User-Agent": UA,
        "Referer": f"https://live.bilibili.com/{args.room_id}",
        "Origin": "https://live.bilibili.com",
    }

    print(f"房间   : {args.room_id}")
    print(f"账号   : {payload.get('uname')} (uid={payload.get('uid')})")
    print(f"内容   : {text}   [{len(text)} 字, 颜色 {args.color}, 模式 {args.mode}]")

    if args.dry_run:
        print("\n--dry-run，未发送。请求体如下:")
        shown = dict(form)
        shown["csrf"] = shown["csrf_token"] = csrf[:4] + "***" + csrf[-4:]
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 0

    print("\n正在发送 ...")
    try:
        resp = requests.post(SEND_URL, data=form, headers=headers, cookies=cookies, timeout=20)
        result = resp.json()
    except Exception as exc:
        print(f"请求失败: {exc}")
        return 1

    code = result.get("code")
    mode_info = (result.get("data") or {}).get("mode_info") or {}
    extra_raw = mode_info.get("extra")
    if extra_raw:
        extra = json.loads(extra_raw)
        print(f"回显内容 : {extra.get('content')}")
        print(f"回复关系 : reply_mid={extra.get('reply_mid')} "
              f"reply_uname={extra.get('reply_uname')!r} "
              f"reply_type_enum={extra.get('reply_type_enum')}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    meaning = CODES.get(code, result.get("message") or "")
    print(f"\ncode={code}  {meaning}")
    return 0 if code == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
