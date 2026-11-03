import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from museaigc.graph import GraphError
from museaigc.muse2api import Client, Muse2APIError, TaskRunner
from test_graph import SAMPLE, _png, g  # noqa: F401

PNG = None


class Fake:
    """假 muse2api：记录每次 POST，任务第 2 次查询完成。"""
    def __init__(self, fail=False, poll_errors=0):
        self.posts, self.polls, self.fail, self.poll_errors = [], {}, fail, poll_errors
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def do_POST(self):
                assert self.headers["Authorization"] == "Bearer k"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.posts.append((self.path, self.headers.get("Idempotency-Key"), body))
                tid = f"t{len(fake.posts)}"
                self._j(202, {"id": tid, "status": "queued"})

            def do_GET(self):
                if self.path.startswith("/v1/media/"):
                    src = SAMPLE if self.path.endswith(".mp4") else fake.png
                    data = open(src, "rb").read()
                    self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers(); self.wfile.write(data); return
                tid = self.path.rsplit("/", 1)[1]
                if fake.poll_errors:
                    fake.poll_errors -= 1; return self._j(502, {"error": "bad gateway"})
                n = fake.polls[tid] = fake.polls.get(tid, 0) + 1
                if n < 2:
                    return self._j(200, {"id": tid, "status": "running"})
                if fake.fail:
                    return self._j(200, {"id": tid, "status": "failed", "error": "refused"})
                if "/videos/" in self.path:
                    return self._j(200, {"id": tid, "status": "succeeded", "result": {"url": f"/v1/media/{tid}.mp4"}})
                return self._j(200, {"id": tid, "status": "completed", "url": f"/v1/media/{tid}.png"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def fake(tmp_path):
    f = Fake(); f.png = str(tmp_path / "fake.png"); _png(f.png, "purple")
    yield f
    f.httpd.shutdown()


def runner(gr, fake):
    return TaskRunner(gr, Client(fake.base, "k"), poll_s=0.01, timeout_s=5)


def use_muse2api(gr):
    for n in gr.data["nodes"]:
        if n["type"] in ("keyframe", "video"):
            n["params"]["provider"] = "muse2api"
    gr.save()


def test_keyframe_candidate_then_pick(g, fake):  # noqa: F811
    use_muse2api(g); g.run("C01"); g.run("LOC01")
    t = runner(g, fake)
    rec = g.generate("SH01_kf", tasks=t)
    path, idem, body = fake.posts[0]
    assert path == "/v1/images/tasks" and idem and len(body["images"]) == 2
    assert body["images"][0]["url"].startswith("data:image/png;base64,")
    assert rec["state"] == "DONE" and len(rec["sha256"]) == 64
    assert g.status()["SH01_kf"] == "WAITING_PICK"          # 候选不自动成为首帧
    assert len(g.candidates("SH01_kf", t)) == 1
    g.pick("SH01_kf", rec["file"])
    assert g.status()["SH01_kf"] == "DONE"


def test_rerun_never_reposts(g, fake):  # noqa: F811
    use_muse2api(g); g.run("C01"); g.run("LOC01")
    t = runner(g, fake)
    g.generate("SH01_kf", tasks=t); g.generate("SH01_kf", tasks=t)
    assert len(fake.posts) == 1                               # 同输入再跑：复用，不重发
    g.generate("SH01_kf", tasks=t, new_attempt=True)
    assert len(fake.posts) == 2 and fake.posts[0][1] != fake.posts[1][1]   # 新尝试 = 新幂等键


def test_poll_errors_are_retried_without_repost(g, tmp_path):  # noqa: F811
    f = Fake(poll_errors=3); f.png = str(tmp_path / "p.png"); _png(f.png)
    try:
        use_muse2api(g); g.run("C01"); g.run("LOC01")
        g.generate("SH01_kf", tasks=runner(g, f))
        assert len(f.posts) == 1
    finally:
        f.httpd.shutdown()


def test_failed_is_terminal(g, tmp_path):  # noqa: F811
    f = Fake(fail=True); f.png = str(tmp_path / "p.png"); _png(f.png)
    try:
        use_muse2api(g); g.run("C01"); g.run("LOC01")
        t = runner(g, f)
        with pytest.raises(Muse2APIError, match="refused"):
            g.generate("SH01_kf", tasks=t)
        with pytest.raises(Muse2APIError, match="new-attempt"):
            g.generate("SH01_kf", tasks=t)
        assert len(f.posts) == 1
    finally:
        f.httpd.shutdown()


def test_ambiguous_submit_is_not_retried(g, fake):  # noqa: F811
    use_muse2api(g); g.run("C01"); g.run("LOC01")
    t = runner(g, fake)
    key = g.key("SH01_kf")
    t._save({"node": "SH01_kf", "kind": "image", "attempt": 1, "key": key,
             "idempotencyKey": "x", "state": "SUBMITTING"})   # 模拟提交时崩溃
    with pytest.raises(Muse2APIError, match="结果不明"):
        g.generate("SH01_kf", tasks=t)
    assert fake.posts == []


def test_video_requires_send_and_binds(g, fake, tmp_path):  # noqa: F811
    use_muse2api(g); g.run("C01"); g.run("LOC01")
    _png(str(tmp_path / "kf.png"), "green"); g.pick("SH01_kf", "kf.png")
    t = runner(g, fake)
    with pytest.raises(GraphError, match="sendAllowed"):
        g.generate("SH01_v", tasks=t, send=True)
    g.nodes()["SH01_v"]["params"]["sendAllowed"] = True; g.save()
    with pytest.raises(GraphError):
        g.generate("SH01_v", tasks=t, send=False)
    assert fake.posts == []
    r = g.generate("SH01_v", tasks=t, send=True)
    path, _, body = fake.posts[0]
    assert path == "/v1/videos" and len(body["images"]) == 1 and body["duration"] == 5
    assert r["take"] == "t1" and g.status()["SH01_v"] == "DONE"
    assert g.generate("SH01_v", tasks=t, send=True)["cached"] and len(fake.posts) == 1
    g.run("SH01_tail")                                         # 下游照常接力
    assert g.status()["SH02_v"] == "WAITING_SEND"


def test_key_not_in_graph_or_records(g, fake):  # noqa: F811
    use_muse2api(g); g.run("C01"); g.run("LOC01")
    g.generate("SH01_kf", tasks=runner(g, fake))
    blob = open(g.path, encoding="utf-8").read()
    for root, _, files in os.walk(g.work):
        for f in files:
            if f.endswith(".json"):
                blob += open(os.path.join(root, f), encoding="utf-8").read()
    assert "Bearer" not in blob and '"k"' not in blob


def test_server_generate_gate(g, fake):  # noqa: F811
    import http.client, time
    from museaigc.canvas_server import make_handler
    use_muse2api(g); g.run("C01"); g.run("LOC01")
    for allow, expect in ((False, 403), (True, 202)):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            g.path, allow_generate=allow, tasks_factory=lambda gr: runner(gr, fake)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        c.request("POST", "/api/gen/SH01_kf", body="{}", headers={"Content-Type": "application/json"})
        assert c.getresponse().status == expect
        if allow:
            for _ in range(100):
                c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
                c.request("GET", "/api/candidates/SH01_kf"); j = json.loads(c.getresponse().read())
                if j["job"]["state"] != "running":
                    break
                time.sleep(0.05)
            assert j["job"]["state"] == "done" and len(j["candidates"]) == 1
        httpd.shutdown()


def test_media_url_rewrites_server_loopback():
    c = Client("https://x.trycloudflare.com", "k")
    assert c.media_url("http://127.0.0.1:8787/v1/media/a.webp") == "https://x.trycloudflare.com/v1/media/a.webp"
    assert c.media_url("/v1/media/a.webp") == "https://x.trycloudflare.com/v1/media/a.webp"
    assert c.media_url("https://cdn.example.com/a.webp") == "https://cdn.example.com/a.webp"
    assert Client("http://127.0.0.1:8787", "k").media_url("http://127.0.0.1:8787/v1/media/a") == "http://127.0.0.1:8787/v1/media/a"


class FakeOpen:
    """假产品 API（/api/open/v1）：multipart 上传，task_id，pending→processing→done。"""
    def __init__(self, png, used=0, quota=30):
        self.posts, self.polls, self.png, self.used, self.quota = [], {}, png, used, quota
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def do_POST(self):
                assert self.headers["Authorization"] == "Bearer k"
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                fake.posts.append((self.path, self.headers["Content-Type"], raw))
                fake.used += 1
                self._j(200, {"task_id": f"o{len(fake.posts)}", "status": "pending", "poll_url": "x"})

            def do_GET(self):
                if self.path == "/api/open/v1/points":
                    return self._j(200, {"points": 10, "daily_quota": fake.quota, "today_used": fake.used})
                if self.path.startswith("/files/"):
                    src = SAMPLE if self.path.endswith(".mp4") else fake.png
                    self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers(); self.wfile.write(open(src, "rb").read()); return
                tid = self.path.rsplit("/", 1)[1]
                n = fake.polls[tid] = fake.polls.get(tid, 0) + 1
                if n < 2:
                    return self._j(200, {"task_id": tid, "status": "processing", "result": None})
                ext = ".mp4" if fake.posts[int(tid[1:]) - 1][0].endswith("img2video") else ".webp"
                return self._j(200, {"task_id": tid, "status": "done",
                                     "result": {"urls": [f"http://127.0.0.1:8787/files/{tid}{ext}"]}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"


def test_open_api_img2img_multipart_and_video(g, tmp_path):  # noqa: F811
    png = str(tmp_path / "o.png"); _png(png, "orange")
    f = FakeOpen(png)
    try:
        use_muse2api(g); g.run("C01"); g.run("LOC01")
        t = TaskRunner(g, Client(f.base, "k", api="open"), poll_s=0.01, timeout_s=5)
        rec = g.generate("SH01_kf", tasks=t)
        path, ctype, raw = f.posts[0]
        assert path == "/api/open/v1/images/img2img" and ctype.startswith("multipart/form-data")
        assert raw.count(b'name="image"') == 2 and b'name="prompt"' in raw
        assert rec["state"] == "DONE" and rec["file"].endswith(".webp")
        g.pick("SH01_kf", rec["file"])
        g.nodes()["SH01_v"]["params"]["sendAllowed"] = True; g.save()
        r = g.generate("SH01_v", tasks=t, send=True)
        assert f.posts[1][0] == "/api/open/v1/videos/img2video" and f.posts[1][2].count(b'name="image"') == 1
        assert r["take"] == "o2" and g.status()["SH01_v"] == "DONE"
    finally:
        f.httpd.shutdown()


def test_open_api_refuses_when_quota_used_up(g, tmp_path):  # noqa: F811
    png = str(tmp_path / "o.png"); _png(png)
    f = FakeOpen(png, used=30, quota=30)
    try:
        use_muse2api(g); g.run("C01"); g.run("LOC01")
        t = TaskRunner(g, Client(f.base, "k", api="open"), poll_s=0.01, timeout_s=5)
        with pytest.raises(Muse2APIError, match="额度"):
            g.generate("SH01_kf", tasks=t)
        assert f.posts == []
    finally:
        f.httpd.shutdown()


def test_quota_refusal_leaves_no_ambiguous_record(g, tmp_path):  # noqa: F811
    png = str(tmp_path / "o.png"); _png(png)
    f = FakeOpen(png, used=30, quota=30)
    try:
        use_muse2api(g); g.run("C01"); g.run("LOC01")
        t = TaskRunner(g, Client(f.base, "k", api="open"), poll_s=0.01, timeout_s=5)
        with pytest.raises(Muse2APIError):
            g.generate("SH01_kf", tasks=t)
        assert t.records("SH01_kf") == []
        f.used = 0
        assert g.generate("SH01_kf", tasks=t)["state"] == "DONE"   # 额度恢复后可正常提交
    finally:
        f.httpd.shutdown()
