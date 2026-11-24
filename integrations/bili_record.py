#!/usr/bin/env python3
"""录制直播音频，按时间戳切片存档，供离线实验使用。

为什么需要它：直播间只有开播时才有音频，调参（VAD 阈值、歌词过滤、
分离效果）不可能总等着开播。录一段长的带时间戳音频，之后随时可回放实验。

切片文件名自带开始时刻，例如 rec_20261014_163512.wav。

用法:
    python -m integrations.bili_record --room-id 123456789 --minutes 60
    python -m integrations.bili_record --room-id 123456789 --minutes 60 --wait-live
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


from .bili_live_asr import UA, get_stream_url, load_cookies  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def record_once(stream_url, out_dir, seconds, seg_seconds, title):
    """跑一段 ffmpeg，返回是否正常结束。"""
    pattern = str(out_dir / "rec_%Y%m%d_%H%M%S.wav")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-user_agent", UA,
        "-referer", "https://live.bilibili.com/",
        "-i", stream_url,
        "-t", str(int(seconds)),
        "-vn",
        "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
        "-f", "segment",
        "-segment_time", str(seg_seconds),
        "-reset_timestamps", "1",
        "-strftime", "1",
        pattern,
    ]
    creation = 0x08000000 if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=creation)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        raise
    err = proc.stderr.read().decode("utf-8", "replace").strip()
    if err:
        print("  ffmpeg: %s" % err[:200])
    return proc.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="录制直播音频（带时间戳切片）")
    ap.add_argument("--room-id", type=int, required=True)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--seg-seconds", type=int, default=300,
                    help="每个切片时长，默认 300 秒")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--cookie-file", default=str(
        Path(__file__).resolve().parent.parent / "bili_cookies.json"))
    ap.add_argument("--wait-live", action="store_true")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent.parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else (
        here / "audio" / ("%d_%s" % (args.room_id, stamp)))
    out_dir.mkdir(parents=True, exist_ok=True)

    cookies = load_cookies(Path(args.cookie_file))
    deadline = time.time() + args.minutes * 60
    meta_path = out_dir / "session.json"
    meta = {"room_id": args.room_id, "started": datetime.now().isoformat(),
            "target_minutes": args.minutes, "segment_seconds": args.seg_seconds,
            "segments": []}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print("输出目录: %s" % out_dir)

    rounds = 0
    while time.time() < deadline:
        remain = deadline - time.time()
        url = get_stream_url(args.room_id, cookies)
        while url is None:
            if not args.wait_live:
                print("房间未开播，退出（可加 --wait-live 等待）。")
                return 1
            print("[%s] 未开播，30s 后重试 ..." % time.strftime("%H:%M:%S"))
            time.sleep(30)
            url = get_stream_url(args.room_id, cookies)
        rounds += 1
        print("[%s] 开始录制第 %d 段，剩余 %.0f 分钟" % (
            time.strftime("%H:%M:%S"), rounds, remain / 60))
        record_once(url, out_dir, remain, args.seg_seconds, args.room_id)
        if time.time() < deadline - 1:
            print("[%s] 连接中断，5 秒后重新取流续录 ..." % time.strftime("%H:%M:%S"))
            time.sleep(5)

    files = sorted(out_dir.glob("rec_*.wav"))
    # ffmpeg 被 -t 截断时会在末尾留一个 0 字节文件，清掉
    for f in list(files):
        if f.stat().st_size == 0:
            try:
                f.unlink()
            except OSError:
                pass
    files = sorted(out_dir.glob("rec_*.wav"))
    total = sum(f.stat().st_size for f in files)
    meta["segments"] = [f.name for f in files]
    meta["finished"] = datetime.now().isoformat()
    meta["file_count"] = len(files)
    meta["total_mb"] = round(total / 1024 / 1024, 1)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print("-" * 60)
    print("录制结束：%d 个切片，共 %.1f MB" % (len(files), total / 1024 / 1024))
    print("清单: %s" % meta_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
