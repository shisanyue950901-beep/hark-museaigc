"""本机画布服务：只监听 127.0.0.1。画布里可以编辑、保存、运行本机节点、pick；
视频节点（会消耗 Muse 次数）只能在终端用 `museaigc graph run <图> <节点> --send` 执行。"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .graph import LOCAL_TYPES, SPEC, Graph, GraphError

HTML = os.path.join(os.path.dirname(__file__), "canvas.html")


def make_handler(graph_path: str):
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code: int, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _graph(self):
            return Graph.load(graph_path)

        def _local_only(self):
            # 拒绝跨站请求：画布只服务本机页面
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                self._json(403, {"error": "cross-origin"}); return False
            return True

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                with open(HTML, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers(); self.wfile.write(body); return
            if u.path == "/api/graph":
                g = self._graph()
                return self._json(200, {"graph": g.data, "spec": {k: {"in": {p: list(v) for p, v in s["in"].items()}, "out": s["out"]} for k, s in SPEC.items()}})
            if u.path == "/api/status":
                g = self._graph()
                try:
                    warns = g.validate(); st = g.status(); err = None
                except GraphError as e:
                    warns, st, err = [], {}, str(e)
                outs = {nid: g.state().get(nid) for nid in st if st[nid] == "DONE"}
                return self._json(200, {"status": st, "warnings": warns, "error": err, "outputs": outs})
            if u.path.startswith("/api/brief/"):
                return self._json(200, self._graph().brief(unquote(u.path[11:])))
            if u.path.startswith("/files/"):
                g = self._graph()
                p = os.path.normpath(os.path.join(g.dir, unquote(u.path[7:])))
                if not p.startswith(g.dir + os.sep) or not os.path.isfile(p):
                    return self._json(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(p)[0] or "application/octet-stream")
                self.end_headers()
                with open(p, "rb") as f:
                    self.wfile.write(f.read())
                return
            self._json(404, {"error": "not found"})

        def do_PUT(self):
            if not self._local_only():
                return
            if urlparse(self.path).path != "/api/graph":
                return self._json(404, {"error": "not found"})
            with lock:
                g = self._graph()
                old = g.data
                g.data = self._body()
                try:
                    warns = g.validate()
                except GraphError as e:
                    g.data = old
                    return self._json(400, {"error": str(e)})
                g.save()
            self._json(200, {"ok": True, "warnings": warns})

        def do_POST(self):
            if not self._local_only():
                return
            u = urlparse(self.path)
            with lock:
                g = self._graph()
                try:
                    if u.path.startswith("/api/run/"):
                        nid = unquote(u.path[9:])
                        t = g.nodes().get(nid, {}).get("type")
                        if t not in LOCAL_TYPES | {"ref"}:
                            return self._json(403, {"error": f"{t} 节点不能在画布里运行；视频请在终端执行 graph run --send，首帧请 pick"})
                        return self._json(200, g.run(nid))
                    if u.path.startswith("/api/pick/"):
                        return self._json(200, g.pick(unquote(u.path[10:]), self._body()["file"]))
                except GraphError as e:
                    return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

    return H


def serve(graph_path: str, port: int = 8790):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(os.path.abspath(graph_path)))
    print(f"画布已启动：http://127.0.0.1:{httpd.server_address[1]}/  （Ctrl+C 退出）")
    httpd.serve_forever()
