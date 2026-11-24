"""标注页面 + 自动保存：标完直接写盘，不用复制粘贴。

用法: python experiments/labeling/serve.py [端口]
然后浏览器打开 http://127.0.0.1:8771
"""

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
LABELS = HERE / "labels.json"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send((HERE / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif path.startswith("/clips/"):
            f = HERE / path.lstrip("/")
            if f.exists() and f.suffix == ".wav":
                self._send(f.read_bytes(), "audio/wav")
            else:
                self._send(b"not found", "text/plain", 404)
        elif path == "/labels":
            body = LABELS.read_bytes() if LABELS.exists() else b"{}"
            self._send(body, "application/json; charset=utf-8")
        elif path == "/clips.json":
            f = HERE / "clips.json"
            body = f.read_bytes() if f.exists() else b"[]"
            self._send(body, "application/json; charset=utf-8")
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        if self.path.split("?")[0] != "/labels":
            self._send(b"not found", "text/plain", 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
        except Exception:
            self._send(b'{"ok":false}', "application/json")
            return
        data["saved_at"] = datetime.now().isoformat(timespec="seconds")
        LABELS.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print("[%s] 已保存 %d 条标注 -> %s" % (
            datetime.now().strftime("%H:%M:%S"),
            len(data.get("labels") or {}), LABELS), flush=True)
        self._send(b'{"ok":true}', "application/json")


class Server(ThreadingHTTPServer):
    allow_reuse_address = False


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8771
    try:
        httpd = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise SystemExit("端口 %d 被占用: %s" % (port, exc))
    print("标注页面: http://127.0.0.1:%d" % port, flush=True)
    print("标注会实时保存到: %s" % LABELS, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
