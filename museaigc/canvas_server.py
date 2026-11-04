"""本机画布服务：只监听 127.0.0.1。画布里可以编辑、保存、运行本机节点、pick；
默认不能在画布里消耗 Muse 次数；用 `graph serve --allow-generate` 启动后，
首帧/视频节点（provider=muse2api）可点「生成」，视频仍需节点 sendAllowed=true。"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .graph import LOCAL_TYPES, SPEC, Graph, GraphError

HTML = os.path.join(os.path.dirname(__file__), "canvas.html")


def make_handler(graph_path: str, allow_generate: bool = False, tasks_factory=None):
    gp = graph_path if callable(graph_path) else (lambda: graph_path)
    lock = threading.Lock()
    jobs: dict = {}   # nid -> {"state": running|done|error, "result"|"error"}

    gen_slots = threading.BoundedSemaphore(2)   # 同时最多 2 个生成，低于服务端 max_running

    def _gen(nid: str, send: bool, new: bool = False):
        with gen_slots:
            try:
                g = Graph.load(gp())
                tasks = tasks_factory(g) if tasks_factory else None
                jobs[nid] = {"state": "done", "result": g.generate(nid, tasks=tasks, send=send, new_attempt=new)}
            except Exception as e:  # noqa: BLE001 — 结果回给画布
                jobs[nid] = {"state": "error", "error": str(e)}

    def start_gen(nid: str, send: bool, new: bool = False) -> bool:
        """已在跑/排队就不重复起。返回是否新起了任务。"""
        if (jobs.get(nid) or {}).get("state") in ("running", "queued"):
            return False
        jobs[nid] = {"state": "running"}
        threading.Thread(target=_gen, args=(nid, send, new), daemon=True).start()
        return True

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
            return Graph.load(gp())

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
            if u.path.startswith("/api/candidates/"):
                nid = unquote(u.path[16:])
                g = self._graph()
                try:
                    c = g.candidates(nid)
                except Exception:
                    c = []
                task = None
                try:
                    from .muse2api import TaskRunner
                    recs = TaskRunner(g).records(nid)
                    if recs:
                        r = recs[-1]
                        task = {k: r.get(k) for k in ("attempt", "state", "status", "stage", "cost_points", "width",
                                                      "height", "error", "lastError", "retryable", "recoverError")}
                        task["attempts"] = len(recs)
                except Exception:  # noqa: BLE001
                    pass
                return self._json(200, {"candidates": c, "job": jobs.get(nid), "task": task,
                                        "allowGenerate": allow_generate})
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
                    if u.path.startswith("/api/gen/"):
                        nid = unquote(u.path[9:])
                        if not allow_generate:
                            return self._json(403, {"error": "画布未开启生成：用 graph serve --allow-generate 启动"})
                        b = self._body()
                        send = bool(b.get("send"))
                        if not start_gen(nid, send, bool(b.get("new"))):
                            return self._json(409, {"error": "该节点正在生成"})
                        return self._json(202, {"state": "running"})
                    if u.path.startswith("/api/cancel/"):
                        from .muse2api import Client, TaskRunner
                        nid = unquote(u.path[12:])
                        runner = tasks_factory(g) if tasks_factory else TaskRunner(g, Client(api="open"))
                        recs = runner.records(nid)
                        if not recs or recs[-1].get("state") != "SUBMITTED":
                            return self._json(409, {"error": "这个节点没有进行中的任务"})
                        return self._json(200, runner.client.cancel(recs[-1]["taskId"]))
                    if u.path.startswith("/api/pick/"):
                        return self._json(200, g.pick(unquote(u.path[10:]), self._body()["file"]))
                except GraphError as e:
                    return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

    H.jobs = jobs
    H.start_gen = staticmethod(start_gen)
    H.lock = lock
    return H


def serve(graph_path: str, port: int = 8790, allow_generate: bool = False):
    httpd = ThreadingHTTPServer(("127.0.0.1", port),
                                make_handler(os.path.abspath(graph_path), allow_generate=allow_generate))
    print(f"画布已启动：http://127.0.0.1:{httpd.server_address[1]}/  （Ctrl+C 退出）")
    httpd.serve_forever()
