#!/usr/bin/env python3
"""B 站扫码登录：把登录 Cookie（含 SESSDATA）保存到本地文件，供弹幕脚本使用。

流程:
    1. 调 passport 接口生成登录二维码 (qrcode_key)
    2. 用 segno 把二维码渲染成本地 PNG，手机 B 站 App 扫码并确认
    3. 轮询 poll 接口，拿到 Set-Cookie
    4. 用 nav 接口验证登录态，打印 uid / 昵称

用法:
    python bili_login.py
    python bili_login.py --out D:\\somewhere\\bili_cookies.json

注意:
    bili_cookies.json 等同于你的登录凭证，不要外传；测完可以在 B 站退出登录使其失效。
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests
import segno

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def mask(value: str) -> str:
    if not value:
        return "(空)"
    if len(value) <= 10:
        return value[:2] + "***"
    return value[:6] + "***" + value[-4:]


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="B 站扫码登录")
    ap.add_argument("--out", default=str(here / "bili_cookies.json"))
    ap.add_argument("--qr", default=str(here / "bili_login_qr.png"))
    ap.add_argument("--timeout", type=int, default=180, help="等待扫码的秒数")
    args = ap.parse_args()

    sess = requests.Session()
    sess.headers.update(HEADERS)

    print("正在生成登录二维码 ...")
    try:
        gen = sess.get(GEN_URL, timeout=15).json()
    except Exception as exc:
        print(f"请求失败: {exc}")
        return 1
    if gen.get("code") != 0:
        print(f"生成二维码失败: {gen}")
        return 1

    url = gen["data"]["url"]
    qrcode_key = gen["data"]["qrcode_key"]

    segno.make(url, error="m").save(args.qr, scale=8, border=2)
    print(f"二维码图片: {args.qr}")
    print("请用手机 B 站 App 扫码并确认登录（约 3 分钟内有效）")

    deadline = time.time() + args.timeout
    last_status = None
    while True:
        if time.time() > deadline:
            print("等待扫码超时，请重新运行。")
            return 3
        try:
            poll = sess.get(POLL_URL, params={"qrcode_key": qrcode_key}, timeout=15).json()
        except Exception as exc:
            print(f"轮询出错: {exc}")
            time.sleep(2)
            continue

        if poll.get("code") != 0:
            print(f"  接口异常: {poll}")
            time.sleep(2)
            continue
        # 注意: 外层 code 是接口状态，登录状态在内层 data.code
        # 86101=未扫码  86090=已扫码待确认  0=登录成功  86038=二维码过期
        data = poll.get("data") or {}
        code = data.get("code")
        message = data.get("message")
        if (code, message) != last_status:
            print(f"  状态: code={code} message={message}")
            last_status = (code, message)

        if code == 0:
            break
        if code == 86038:
            print("二维码已过期，请重新运行。")
            return 2
        time.sleep(2)

    jar = sess.cookies.get_dict()
    if not jar:
        print("登录成功但没拿到 Cookie，接口可能变了。")
        return 4

    sess.headers.update({"Referer": "https://live.bilibili.com/"})
    try:
        nav = sess.get(NAV_URL, timeout=15).json()
    except Exception as exc:
        nav = {"code": -1, "message": str(exc)}
    is_login = (nav.get("data") or {}).get("isLogin")
    mid = (nav.get("data") or {}).get("mid")
    uname = (nav.get("data") or {}).get("uname")

    payload = {
        "cookies": jar,
        "uid": mid,
        "uname": uname,
        "is_login": bool(is_login),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    out_path = Path(args.out)
    if out_path.exists():
        # 换账号前把旧凭证留一份，方便切回去
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
            old_name = old.get("uname") or "prev"
        except Exception:
            old_name = "prev"
        safe = "".join(ch for ch in str(old_name) if ch.isalnum() or ch in "_-")
        backup = out_path.with_name(f"{out_path.stem}_{safe}{out_path.suffix}")
        if backup.exists():
            backup = out_path.with_name(
                f"{out_path.stem}_{safe}_{time.strftime('%H%M%S')}{out_path.suffix}"
            )
        out_path.rename(backup)
        print(f"已备份原账号凭证: {backup.name}")

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("")
    print("登录成功。")
    print(f"  uid    : {mid}")
    print(f"  昵称   : {uname}")
    print(f"  SESSDATA: {mask(jar.get('SESSDATA', ''))}  (完整值只写在文件里，不打印)")
    print(f"  Cookie : {args.out}")
    print("")
    print("接下来可以用它跑弹幕脚本，例如:")
    print(f"  powershell -File bili_danmaku.ps1 -RoomId 123456789 -Seconds 120 -CookieFile {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
