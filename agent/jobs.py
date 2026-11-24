"""后台转录任务：把 bili_live_asr.py 作为子进程跑起来，输出写日志供页面轮询。

测试页面用它"用最新算法重跑已录制的内容"。
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PYTHON = sys.executable


def list_recordings(audio_dir="audio"):
    """列出已录制的音频场次。"""
    base = ROOT / audio_dir
    out = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        files = sorted(d.glob("rec_*.wav"))
        if not files:
            continue
        total = sum(f.stat().st_size for f in files)
        meta = {}
        mp = d / "session.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        out.append({
            "dir": d.name,
            "started": meta.get("started") or files[0].stem.replace("rec_", ""),
            "files": len(files),
            "mb": round(total / 1024 / 1024, 1),
            "has_result": (d / "transcript.txt").exists(),
        })
    return out


class TranscribeJob:
    """用最新算法转录一个录制目录。"""

    def __init__(self, rec_dir, room_id, extra_args=None):
        self.dir = ROOT / "audio" / rec_dir
        self.name = rec_dir
        self.log = self.dir / "transcribe.log"
        self.out = self.dir / "transcript.txt"
        self.manifest = self.dir / "transcript_meta.json"
        self.proc = None
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.extra = extra_args or []
        self.room_id = room_id

    def start(self):
        self.log.write_text("", encoding="utf-8")
        self.out.write_text("", encoding="utf-8")
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self.started_at = time.time()
        return self

    def _run(self):
        files = sorted(self.dir.glob("rec_*.wav"))
        # 合并成一个文件：单个文件跑，时间轴才是连续的
        combined = self.dir / "_combined.wav"
        if not combined.exists():
            lst = self.dir / "_concat.txt"
            lst.write_text("".join("file '%s'\n" % f.name for f in files),
                           encoding="utf-8")
            with open(self.log, "a", encoding="utf-8") as lg:
                lg.write("合并 %d 个切片 ...\n" % len(files))
                lg.flush()
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "concat", "-safe", "0", "-i", str(lst),
                     "-c", "copy", str(combined)],
                    cwd=str(self.dir), stdout=lg, stderr=subprocess.STDOUT)
        cmd = [PYTHON, "-u", "-m", "integrations.bili_live_asr",
               "--room-id", str(self.room_id or 0),
               "--input-file", str(combined),
               "--model", "Systran/faster-whisper-large-v3",
               "--out", str(self.out)] + self.extra
        env = {"KMP_DUPLICATE_LIB_OK": "TRUE"}
        import os
        e = dict(os.environ)
        e.update(env)
        with open(self.log, "a", encoding="utf-8") as lg:
            lg.write("命令: %s\n\n" % " ".join(cmd[1:]))
            lg.flush()
            self.proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=lg, stderr=subprocess.STDOUT,
                env=e, creationflags=0x08000000 if os.name == "nt" else 0)
            self.proc.wait()
            self.exit_code = self.proc.returncode
        self.finished_at = time.time()
        self.manifest.write_text(json.dumps({
            "dir": self.name, "files": len(files),
            "exit_code": self.exit_code,
            "seconds": round(self.finished_at - self.started_at, 1),
            "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def status(self, tail=40):
        running = self.proc is not None and self.proc.poll() is None
        lines = []
        if self.log.exists():
            lines = self.log.read_text(encoding="utf-8",
                                       errors="replace").splitlines()[-tail:]
        kept = 0
        if self.out.exists():
            kept = sum(1 for l in self.out.read_text(
                encoding="utf-8", errors="replace").splitlines() if l.strip())
        return {"name": self.name, "running": running,
                "exit_code": self.exit_code, "kept": kept,
                "elapsed": round(time.time() - self.started_at, 1)
                if self.started_at else 0,
                "log": lines}
