"""自用版桌面应用：双击启动 → 本机服务 → 浏览器打开。

- 工作区：默认是程序目录下的 workspace/（便携），可用 MUSEAIGC_HOME 指定。
  workspace/projects/<工程名>/graph.json   每个工程一个画布
  workspace/settings.json                    接口地址、接口类型（不含 Key）
  workspace/key.bin                          Key：Windows 用 DPAPI 加密（只有当前用户能解）
- 只监听 127.0.0.1；拒绝跨站请求；Key 不进日志、不进工程文件、不回传给页面。
- 生成纪律沿用 muse2api.TaskRunner：先落记录再提交，结果不明不自动补发。
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import secretstore
from .canvas_server import make_handler

APP_HTML = os.path.join(os.path.dirname(__file__), "app.html")
NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\-· ]{1,40}$")

STARTER = {
    "version": 1,
    "name": "",
    "nodes": [
        {"id": "REF1", "type": "ref", "label": "参考图 1", "x": 40, "y": 60, "params": {}},
        {"id": "KF1", "type": "keyframe", "label": "首帧 1", "x": 300, "y": 60,
         "params": {"provider": "muse-open", "size": "16:9", "prompt": ""}},
        {"id": "V1", "type": "video", "label": "视频 1", "x": 560, "y": 60,
         "params": {"provider": "muse-open", "aspect": "16:9", "durationRequestSeconds": 5,
                    "sendAllowed": True, "prompt": ""}},
    ],
    "edges": [
        {"from": "REF1", "to": "KF1", "port": "refs"},
        {"from": "KF1", "to": "V1", "port": "frame"},
    ],
}


def default_home() -> str:
    if os.environ.get("MUSEAIGC_HOME"):
        return os.path.abspath(os.environ["MUSEAIGC_HOME"])
    base = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "_museaigc_portable", False) \
        else os.getcwd()
    return os.path.join(base, "workspace")


class App:
    def __init__(self, home: str | None = None):
        self.home = home or default_home()
        self.projects = os.path.join(self.home, "projects")
        os.makedirs(self.projects, exist_ok=True)
        self.store = secretstore.KeyStore(os.path.join(self.home, "key.bin"))
        self.current: str | None = None
        self.apply_settings()

    # ---------- 设置 ----------
    def settings(self) -> dict:
        p = os.path.join(self.home, "settings.json")
        try:
            with open(p, encoding="utf-8") as f:
                s = json.load(f)
        except (FileNotFoundError, ValueError):
            s = {}
        s.setdefault("base", "http://127.0.0.1:8787")
        s.setdefault("api", "open")
        return s

    def save_settings(self, base: str, api: str, key: str | None):
        base = base.strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", base):
            raise ValueError("接口地址要以 http:// 或 https:// 开头")
        if api not in ("open", "openai"):
            raise ValueError("接口类型只能是 open 或 openai")
        with open(os.path.join(self.home, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"base": base, "api": api}, f, ensure_ascii=False, indent=2)
        if key:
            self.store.save(key.strip())
        self.apply_settings()

    def apply_settings(self):
        s = self.settings()
        ff = ffmpeg_path()
        if ff:
            os.environ["MUSEAIGC_FFMPEG"] = ff
        os.environ["MUSE2API_BASE"] = s["base"]
        os.environ["MUSE2API_API"] = s["api"]
        k = self.store.load()
        if k:
            os.environ["MUSE2API_KEY"] = k        # 只在本进程内存里
        else:
            os.environ.pop("MUSE2API_KEY", None)

    def public_settings(self) -> dict:
        s = self.settings()
        return {**s, "hasKey": bool(self.store.load()), "keyStorage": self.store.kind,
                "ffmpeg": ffmpeg_path() or None, "home": self.home}

    def test_connection(self) -> dict:
        from .muse2api import Client
        c = Client()
        if c.api == "open":
            _, j = c._req("GET", "/api/open/v1/points")
            return {"ok": True, "points": j.get("points"), "daily_quota": j.get("daily_quota"),
                    "today_used": j.get("today_used")}
        _, j = c._req("GET", "/v1/models")
        return {"ok": True, "models": [m.get("id") for m in j.get("data", [])]}

    # ---------- 工程 ----------
    def list_projects(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.projects)):
            g = os.path.join(self.projects, name, "graph.json")
            if os.path.isfile(g):
                out.append({"name": name, "mtime": int(os.path.getmtime(g))})
        out.sort(key=lambda x: -x["mtime"])
        return out

    def create_project(self, name: str) -> str:
        name = name.strip()
        if not NAME_RE.match(name):
            raise ValueError("工程名 1–40 个字，只能用中文、字母、数字、空格、-、_、·")
        d = os.path.join(self.projects, name)
        if os.path.exists(d):
            raise ValueError("已有同名工程")
        os.makedirs(os.path.join(d, "refs"))
        g = json.loads(json.dumps(STARTER)); g["name"] = name
        with open(os.path.join(d, "graph.json"), "w", encoding="utf-8") as f:
            json.dump(g, f, ensure_ascii=False, indent=2)
        return name

    def open_project(self, name: str):
        p = os.path.join(self.projects, name, "graph.json")
        if not NAME_RE.match(name) or not os.path.isfile(p):
            raise ValueError("工程不存在")
        self.current = name

    def graph_path(self) -> str:
        if not self.current:
            raise ValueError("还没打开工程")
        return os.path.join(self.projects, self.current, "graph.json")

    def save_upload(self, node: str, filename: str, data: bytes) -> str:
        from .graph import Graph
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            raise ValueError("参考图只支持 png、jpg、webp")
        if len(data) > 30 * 1024 * 1024:
            raise ValueError("图片超过 30MB")
        g = Graph.load(self.graph_path())
        n = g.nodes().get(node)
        if not n or n["type"] != "ref":
            raise ValueError("只能给参考图节点上传")
        rel = os.path.join("refs", f"{node}{ext}")
        with open(os.path.join(g.dir, rel), "wb") as f:
            f.write(data)
        n.setdefault("params", {})["file"] = rel.replace(os.sep, "/")
        n["params"].pop("sha256", None)
        g.save()
        g.run(node)                     # 立刻登记 SHA，下游可用
        return rel


def ffmpeg_path() -> str:
    import shutil
    env = os.environ.get("MUSEAIGC_FFMPEG")
    if env and os.path.isfile(env):
        return env
    here = os.path.dirname(os.path.abspath(sys.executable))
    for cand in (os.path.join(here, "ffmpeg", "ffmpeg.exe"), os.path.join(here, "ffmpeg.exe")):
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffmpeg") or ""


def make_app_handler(app: App, tasks_factory=None):
    Base = make_handler(app.graph_path, allow_generate=True, tasks_factory=tasks_factory)

    class H(Base):
        def _app_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._file(APP_HTML)
            if u.path == "/canvas":
                if not app.current:
                    return self._redirect("/")
                return self._file(os.path.join(os.path.dirname(__file__), "canvas.html"))
            if u.path == "/api/app/state":
                return self._json(200, {"settings": app.public_settings(),
                                        "projects": app.list_projects(), "current": app.current})
            if (u.path.startswith("/api/") or u.path.startswith("/files/")) and not app.current:
                return self._json(409, {"error": "还没打开工程"})
            return super().do_GET()

        def do_POST(self):
            if not self._local_only():
                return
            u = urlparse(self.path)
            try:
                if u.path == "/api/app/settings":
                    j = json.loads(self._app_body() or b"{}")
                    app.save_settings(j.get("base", ""), j.get("api", "open"), j.get("key") or None)
                    return self._json(200, app.public_settings())
                if u.path == "/api/app/test":
                    return self._json(200, app.test_connection())
                if u.path == "/api/app/projects":
                    j = json.loads(self._app_body() or b"{}")
                    name = app.create_project(j.get("name", ""))
                    app.open_project(name)
                    return self._json(200, {"name": name})
                if u.path.startswith("/api/app/open/"):
                    app.open_project(unquote(u.path[len("/api/app/open/"):]))
                    return self._json(200, {"ok": True})
                if u.path.startswith("/api/upload/"):
                    fname = unquote(self.headers.get("X-Filename") or "")
                    rel = app.save_upload(unquote(u.path[len("/api/upload/"):]), fname, self._app_body())
                    return self._json(200, {"file": rel})
            except Exception as e:  # noqa: BLE001 — 错误回给页面，不含 Key
                return self._json(400, {"error": str(e)})
            if not app.current:
                return self._json(409, {"error": "还没打开工程"})
            return super().do_POST()

        def do_PUT(self):
            if not app.current:
                return self._json(409, {"error": "还没打开工程"})
            return super().do_PUT()

        def _file(self, p):
            with open(p, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _redirect(self, to):
            self.send_response(302); self.send_header("Location", to); self.end_headers()

    return H


def _free_port(preferred: int) -> int:
    for p in (preferred, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            s.close()
    raise RuntimeError("没有可用端口")


def _already_running(port: int) -> bool:
    import urllib.request
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(f"http://127.0.0.1:{port}/api/app/state", timeout=1.5) as r:
            return "settings" in json.loads(r.read())
    except Exception:
        return False


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="museaigc-app")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--home")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    if _already_running(a.port):              # 已开着：只打开浏览器，不起第二个实例
        if not a.no_browser:
            webbrowser.open(f"http://127.0.0.1:{a.port}/")
        print("MuseAIGC 已在运行", flush=True)
        return
    app = App(a.home)
    port = _free_port(a.port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_app_handler(app))
    url = f"http://127.0.0.1:{port}/"
    print(f"MuseAIGC 已启动：{url}  工作区：{app.home}", flush=True)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
