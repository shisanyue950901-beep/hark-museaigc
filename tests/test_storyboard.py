"""分镜表解析与展开；批量生成 + 一键裁切拼接（假产品 API + 本机 ffmpeg）。"""
import shutil
import time

import pytest

from museaigc import storyboard
from museaigc.graph import Graph
from test_app import _req, served  # noqa: F401
from test_muse2api import FakeOpen, _png

TEXT = """镜号 | 首帧画面 | 视频动作 | 秒数 | 接续
SH01 | 夜间教室，杨间低头看手机 | 镜头缓慢推近 | 3 |
SH02 | | 他缓缓转头看向窗外 | 2.5 | 接
# 注释行
SH03 | 全景，教室后排 | 走廊灯依次熄灭 | 3 | 切
"""


def test_parse_rows_and_errors():
    rows = storyboard.parse(TEXT)
    assert [r["shot"] for r in rows] == ["SH01", "SH02", "SH03"]
    assert [r["cont"] for r in rows] == [False, True, False] and rows[1]["dur"] == 2.5
    with pytest.raises(storyboard.StoryboardError, match="第一镜"):
        storyboard.parse("A | x | y | 3 | 接")
    with pytest.raises(storyboard.StoryboardError, match="重复"):
        storyboard.parse("A | x | y\nA | x | y")
    with pytest.raises(storyboard.StoryboardError, match="秒数"):
        storyboard.parse("A | x | y | 9")
    assert storyboard.parse("| A | x | y | 3 | |\n|---|---|---|---|---|")[0]["shot"] == "A"   # Markdown 表格


def test_expand_builds_valid_graph(tmp_path):
    data = {"nodes": [{"id": "C01", "type": "ref", "params": {}}, {"id": "LOC", "type": "ref", "params": {}}],
            "edges": []}
    res = storyboard.expand(data, storyboard.parse(TEXT), ["C01", "LOC"])
    g = Graph(str(tmp_path / "g.json"), data)
    g.validate()
    types = {n["id"]: n["type"] for n in data["nodes"]}
    assert types["SH02_tail"] == "tailframe" and "SH02_kf" not in types
    assert len([e for e in data["edges"] if e["to"] == "SH01_kf"]) == 2
    assert len(g.inputs(res["concat"], "clips")) == 3 and res["seconds"] == 8.5


def test_tail_chain_is_capped():
    text = "\n".join(["A | x | y | 3 |"] + [f"B{i} | | y | 3 | 接" for i in range(5)])
    data = {"nodes": [{"id": "R", "type": "ref", "params": {}}], "edges": []}
    res = storyboard.expand(data, storyboard.parse(text), ["R"])
    assert res["notes"] and any(n["id"].endswith("_kf") and n["id"] != "A_kf" for n in data["nodes"])


def test_expand_needs_ref():
    with pytest.raises(storyboard.StoryboardError, match="参考图"):
        storyboard.expand({"nodes": [], "edges": []}, storyboard.parse("A|x|y"), [])


def _wait_jobs(base):
    for _ in range(200):
        _, j = _req(base, "GET", "/api/batch")
        if not any(s == "running" for s in j["jobs"].values()):
            return j["jobs"]
        time.sleep(0.1)
    raise AssertionError("超时")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")
def test_storyboard_to_final_film(served, tmp_path):  # noqa: F811
    app, base = served
    png = str(tmp_path / "r.png"); _png(png, "orange")
    f = FakeOpen(png)
    try:
        _req(base, "POST", "/api/app/settings", {"base": f.base, "api": "open", "key": "k"})
        _req(base, "POST", "/api/app/projects", {"name": "片头"})
        _req(base, "POST", "/api/upload/REF1", raw=open(png, "rb").read(), headers={"X-Filename": "a.png"})
        c, r = _req(base, "POST", "/api/storyboard", {"text": TEXT})
        assert c == 200, r
        fin = r["concat"]
        # 批量首帧：SH01、SH03（SH02 走尾帧）；模板里的 KF1 没提示词会被跳过
        c, r = _req(base, "POST", "/api/batch/gen", {"type": "keyframe"})
        assert sorted(r["started"]) == ["SH01_kf", "SH03_kf"] and r["skipped"][0]["node"] == "KF1"
        assert set(_wait_jobs(base).values()) == {"done"}
        assert _req(base, "POST", "/api/batch/gen", {"type": "keyframe"})[1]["started"] == []   # 不重复出
        for kf in ("SH01_kf", "SH03_kf"):
            _, cand = _req(base, "GET", f"/api/candidates/{kf}")
            assert _req(base, "POST", f"/api/pick/{kf}", {"file": cand["candidates"][0]["file"]})[0] == 200
        assert _req(base, "POST", "/api/batch/gen", {"type": "video"})[0] == 400          # 没确认
        _, r = _req(base, "POST", "/api/batch/gen", {"type": "video", "confirm": True})
        assert sorted(r["started"]) == ["SH01_v", "SH03_v"]
        _wait_jobs(base)
        _, r = _req(base, "POST", "/api/batch/run")
        assert {"SH02_tail", "SH01_cut", "SH03_cut"} <= set(r["done"]) and not r["errors"], r
        assert {w["node"] for w in r["warnings"]} == {"SH01_cut", "SH03_cut"}   # 样片只有 1 秒
        _, r = _req(base, "POST", "/api/batch/gen", {"type": "video", "confirm": True})
        assert r["started"] == ["SH02_v"]
        _wait_jobs(base)
        _, r = _req(base, "POST", "/api/batch/run")
        assert fin in r["done"], r
        _, st = _req(base, "GET", "/api/status")
        assert st["status"][fin] == "DONE"
        kinds = [p[0] for p in f.posts]
        assert kinds.count("/api/open/v1/images/img2img") == 2 and kinds.count("/api/open/v1/videos/img2video") == 3
    finally:
        f.httpd.shutdown()


def test_clean_draft_and_parse():
    raw = "好的，下面是分镜：\n```\n镜号 | 首帧画面 | 视频动作 | 成片秒数 | 接续\n|---|---|---|---|---|\nSH01 | 中景 | 推近 | 3 |\nSH02 | | 转头 | 2 | 接\n```\n希望有帮助"
    t = storyboard.clean_draft(raw)
    assert [r["shot"] for r in storyboard.parse(t)] == ["SH01", "SH02"]


def test_draft_endpoint_uses_chat(served, monkeypatch):  # noqa: F811
    app, base = served
    from museaigc import muse2api
    seen = {}

    def fake_chat(self, messages, model="muse-chat", timeout=180):
        seen["m"] = messages
        return "SH01 | 夜间教室 | 推近 | 3 |\nSH02 | | 抬眼 | 2 | 接"
    monkeypatch.setattr(muse2api.Client, "chat", fake_chat)
    _req(base, "POST", "/api/app/settings", {"base": "http://127.0.0.1:9", "api": "open", "key": "k"})
    _req(base, "POST", "/api/app/projects", {"name": "d"})
    c, j = _req(base, "POST", "/api/storyboard/draft", {"story": "深夜教室", "shots": 2})
    assert c == 200 and j["text"].startswith("SH01") and "大约 2 个镜头" in seen["m"][1]["content"]
    assert _req(base, "POST", "/api/storyboard/draft", {"story": " "})[0] == 400
