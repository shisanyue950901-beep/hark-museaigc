import json
import os
import shutil
import subprocess

import pytest

from museaigc.graph import Graph, GraphError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "mock", "sample.mp4")
EXAMPLE = os.path.join(ROOT, "examples", "opening", "graph.json")
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="no ffmpeg")


def _png(path, color="red"):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:s=64x36",
                    "-frames:v", "1", path], check=True)


@pytest.fixture
def g(tmp_path):
    d = json.load(open(EXAMPLE, encoding="utf-8"))
    (tmp_path / "refs").mkdir()
    _png(str(tmp_path / "refs" / "C01.png"), "red")
    _png(str(tmp_path / "refs" / "LOC01.png"), "blue")
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return Graph.load(str(p))


def fake_runner(calls):
    def run(g, nid, frame, frame_sha, prm, send):
        calls.append((nid, frame_sha, send))
        out = os.path.join(g.work, f"{nid}.mp4"); os.makedirs(g.work, exist_ok=True)
        shutil.copy(SAMPLE, out)
        return {"state": "RECEIPT_BOUND", "take": nid + "-T1", "path": out}
    return run


def test_example_is_valid(g):
    assert g.validate() == []
    assert g.status()["C01"] == "READY"
    assert g.status()["SH01_kf"] == "BLOCKED"


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["edges"].append({"from": "SH01_kf", "to": "SH01_v", "port": "frame"}), "SH01_v.frame"),
    (lambda d: d["edges"].append({"from": "SH01_v", "to": "SH03_v", "port": "frame"}), "需要 image"),
    (lambda d: d["edges"].append({"from": "SH02_v", "to": "SH01_kf", "port": "refs"}), "需要 image"),
    (lambda d: d["edges"].append({"from": "SH01_tail", "to": "SH01_kf", "port": "refs"}), "成环"),
    (lambda d: d["nodes"].append({"id": "X", "type": "magic"}), "未知类型"),
])
def test_invalid_graphs(g, mutate, needle):
    mutate(g.data)
    with pytest.raises(GraphError) as e:
        g.validate()
    assert needle in str(e.value)


def test_keyframe_needs_pick_and_video_needs_browser(g):
    g.run("C01"); g.run("LOC01")
    with pytest.raises(GraphError):
        g.run("SH01_kf")
    with pytest.raises(GraphError):
        g.run("SH01_v")   # 上游首帧未 pick


@needs_ffmpeg
def test_full_chain_with_tail_and_concat(g, tmp_path):
    calls = []
    for n in ("C01", "LOC01"):
        g.run(n)
    _png(str(tmp_path / "kf1.png"), "green"); _png(str(tmp_path / "kf3.png"), "yellow")
    g.pick("SH01_kf", "kf1.png"); g.pick("SH03_kf", "kf3.png")
    assert g.status()["SH01_v"] == "WAITING_SEND"
    r = g.run("SH01_v", fake_runner(calls), send=True)
    assert r["state"] == "RECEIPT_BOUND"
    tail = g.run("SH01_tail")
    assert tail["file"].endswith(".png")
    g.run("SH02_v", fake_runner(calls), send=True)
    assert calls[1][1] == tail["sha256"]        # SH02 首帧 = SH01 尾帧
    g.run("SH03_v", fake_runner(calls), send=True)
    for n in ("SH01_cut", "SH02_cut", "SH03_cut", "OPENING"):
        g.run(n)
    assert g.status()["OPENING"] == "DONE"
    out = g.abspath(g.output("OPENING")["file"])
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", out], capture_output=True, text=True).stdout)
    assert 2.5 < dur < 3.5   # sample 只有 1 秒 → 3 段各 1 秒


@needs_ffmpeg
def test_cache_and_invalidation(g, tmp_path):
    calls = []
    g.run("C01"); g.run("LOC01")
    _png(str(tmp_path / "kf1.png"), "green")
    g.pick("SH01_kf", "kf1.png")
    g.run("SH01_v", fake_runner(calls), send=True)
    assert g.run("SH01_v", fake_runner(calls), send=True).get("cached")
    assert len(calls) == 1                       # 输入不变 → 不再出片
    _png(str(tmp_path / "kf1b.png"), "white")
    g.pick("SH01_kf", "kf1b.png")                # 换首帧 → 下游失效
    assert g.status()["SH01_v"] == "WAITING_SEND"
    assert g.status()["SH01_tail"] == "BLOCKED"


def test_tail_chain_warning(g):
    d = g.data
    d["nodes"] += [{"id": "T2", "type": "tailframe", "params": {}},
                   {"id": "V3", "type": "video", "params": {}},
                   {"id": "T3", "type": "tailframe", "params": {}},
                   {"id": "V4", "type": "video", "params": {}}]
    d["edges"] += [{"from": "SH02_v", "to": "T2", "port": "video"}, {"from": "T2", "to": "V3", "port": "frame"},
                   {"from": "V3", "to": "T3", "port": "video"}, {"from": "T3", "to": "V4", "port": "frame"}]
    assert any("V4" in w for w in g.validate())


def test_server_rejects_invalid_and_video_run(g):
    import http.client, threading
    from http.server import ThreadingHTTPServer
    from museaigc.canvas_server import make_handler
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(g.path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def req(m, u, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request(m, u, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")
    try:
        bad = json.loads(json.dumps(g.data)); bad["edges"].append({"from": "SH01_tail", "to": "SH01_kf", "port": "refs"})
        assert req("PUT", "/api/graph", bad)[0] == 400
        assert Graph.load(g.path).validate() == []          # 原图未被覆盖
        assert req("POST", "/api/run/SH01_v")[0] == 403      # 画布里不能出片
        assert req("POST", "/api/run/C01", headers={"Origin": "http://evil.example"})[0] == 403
        assert req("POST", "/api/run/C01")[0] == 200
        assert req("GET", "/api/status")[1]["status"]["C01"] == "DONE"
        assert req("GET", "/files/../../etc/passwd")[0] == 404
    finally:
        httpd.shutdown()
