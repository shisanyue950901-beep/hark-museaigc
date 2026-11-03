"""自用版 App：工程、设置、Key 不外泄、上传参考图、经产品 API 生成首帧。"""
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from museaigc.app import App, make_app_handler
from test_muse2api import FakeOpen, _png  # noqa: F401


def _req(base, method, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    r = urllib.request.Request(base + path, data=data, method=method, headers=h)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def served(tmp_path, monkeypatch):
    # 先登记再改，测试结束 monkeypatch 会还原（App 会改写这些环境变量）
    for k in ("MUSE2API_KEY", "MUSE2API_BASE", "MUSE2API_API", "MUSEAIGC_FFMPEG"):
        monkeypatch.setenv(k, "x")
        monkeypatch.delenv(k)
    app = App(str(tmp_path / "ws"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_app_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_settings_key_never_returned(served):
    app, base = served
    c, j = _req(base, "POST", "/api/app/settings", {"base": "http://127.0.0.1:9", "api": "open", "key": "sk-secret-123"})
    assert c == 200 and j["hasKey"] is True
    _, st = _req(base, "GET", "/api/app/state")
    assert "sk-secret-123" not in json.dumps(st)
    assert "sk-secret-123" not in open(os.path.join(app.home, "settings.json")).read()
    assert app.store.load() == "sk-secret-123"


def test_bad_settings_rejected(served):
    _, base = served
    c, j = _req(base, "POST", "/api/app/settings", {"base": "ftp://x", "api": "open"})
    assert c == 400


def test_project_lifecycle_and_name_rules(served):
    app, base = served
    assert _req(base, "GET", "/api/graph")[0] == 409            # 没打开工程
    assert _req(base, "POST", "/api/app/projects", {"name": "../evil"})[0] == 400
    c, j = _req(base, "POST", "/api/app/projects", {"name": "咒与鬼 片头"})
    assert c == 200
    c, g = _req(base, "GET", "/api/graph")
    assert c == 200 and {n["id"] for n in g["graph"]["nodes"]} == {"REF1", "KF1", "V1"}
    assert _req(base, "POST", "/api/app/projects", {"name": "咒与鬼 片头"})[0] == 400
    _, st = _req(base, "GET", "/api/app/state")
    assert st["current"] == "咒与鬼 片头" and st["projects"][0]["name"] == "咒与鬼 片头"


def test_cross_origin_post_refused(served):
    _, base = served
    c, _ = _req(base, "POST", "/api/app/settings", {"base": "http://a", "api": "open", "key": "x"},
                headers={"Origin": "http://evil.example"})
    assert c == 403


def test_upload_ref_then_generate_keyframe_via_open_api(served, tmp_path):
    app, base = served
    png = str(tmp_path / "r.png"); _png(png, "orange")
    f = FakeOpen(png)
    try:
        _req(base, "POST", "/api/app/settings", {"base": f.base, "api": "open", "key": "k"})
        _req(base, "POST", "/api/app/projects", {"name": "t1"})
        assert _req(base, "POST", "/api/upload/KF1", raw=open(png, "rb").read(),
                    headers={"X-Filename": "a.png"})[0] == 400       # 上游未完成，不能直接导入首帧
        c, j = _req(base, "POST", "/api/upload/REF1", raw=open(png, "rb").read(),
                    headers={"X-Filename": "a.png", "Content-Type": "application/octet-stream"})
        assert c == 200 and j["file"].endswith("REF1.png")
        _, st = _req(base, "GET", "/api/status")
        assert st["status"]["REF1"] == "DONE" and st["status"]["KF1"] == "WAITING_PICK"
        _, g = _req(base, "GET", "/api/graph")
        g = g["graph"]; g["nodes"][1]["params"]["prompt"] = "夜间教室"
        assert _req(base, "PUT", "/api/graph", g)[0] == 200
        assert _req(base, "POST", "/api/gen/KF1", {"send": False})[0] == 202
        import time
        for _ in range(100):
            _, c = _req(base, "GET", "/api/candidates/KF1")
            if c["job"] and c["job"]["state"] != "running":
                break
            time.sleep(0.1)
        assert c["job"]["state"] == "done", c
        assert len(c["candidates"]) == 1
        assert f.posts[0][0] == "/api/open/v1/images/img2img"
    finally:
        f.httpd.shutdown()


def test_import_existing_keyframe(served, tmp_path):
    app, base = served
    png = str(tmp_path / "r.png"); _png(png, "orange")
    _req(base, "POST", "/api/app/projects", {"name": "imp"})
    _req(base, "POST", "/api/upload/REF1", raw=open(png, "rb").read(), headers={"X-Filename": "a.png"})
    assert _req(base, "POST", "/api/upload/KF1", raw=b"x", headers={"X-Filename": "a.mp4"})[0] == 400
    c, j = _req(base, "POST", "/api/upload/KF1", raw=open(png, "rb").read(), headers={"X-Filename": "k.png"})
    assert c == 200 and j["file"].startswith("imports")
    _, st = _req(base, "GET", "/api/status")
    assert st["status"]["KF1"] == "DONE"
