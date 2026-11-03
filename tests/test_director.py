"""内置导演：设定、取舍记录、改提示词、通读（对话模型用假的）。"""
import json

import pytest

from museaigc import director, muse2api
from test_app import _req, served  # noqa: F401


def test_parse_json_tolerant():
    assert director.parse_json('```json\n{"a":1}\n```') == {"a": 1}
    assert director.parse_json('好的：{"a": [1]} 以上') == {"a": [1]}
    with pytest.raises(ValueError):
        director.parse_json("没有 JSON")


def test_director_flow(served, monkeypatch):  # noqa: F811
    app, base = served
    seen = []

    def fake_chat(self, messages, model="muse-chat", timeout=180):
        seen.append(messages)
        u = messages[-1]["content"]
        if "重写节点" in u:
            return json.dumps({"prompt": "中近景仰拍，冷蓝窗光", "why": ["加机位", "统一光线"]}, ensure_ascii=False)
        if "通读" in u:
            return json.dumps({"issues": [{"node": "KF1", "problem": "光线方向不明", "fix": "写明窗光在左"},
                                          {"node": "NOPE", "problem": "x", "fix": "y"}], "summary": "基本连贯"},
                              ensure_ascii=False)
        return "{}"
    monkeypatch.setattr(muse2api.Client, "chat", fake_chat)
    _req(base, "POST", "/api/app/settings", {"base": "http://127.0.0.1:9", "api": "open", "key": "k"})
    _req(base, "POST", "/api/app/projects", {"name": "导演"})
    _, j = _req(base, "GET", "/api/director/profile")
    assert "镜头规矩" in j["text"]
    assert _req(base, "POST", "/api/director/profile", {"text": "# 画风\n水墨，低饱和"})[0] == 200
    _, g = _req(base, "GET", "/api/graph")
    for n in g["graph"]["nodes"]:
        if n["id"] == "KF1":
            n["params"]["prompt"] = "教室"
    _req(base, "PUT", "/api/graph", g["graph"])
    assert _req(base, "POST", "/api/director/feedback", {"node": "KF1", "verdict": "reject", "note": "太亮"})[0] == 200
    assert _req(base, "POST", "/api/director/feedback", {"node": "KF1", "verdict": "meh"})[0] == 400
    c, r = _req(base, "POST", "/api/director/revise/KF1", {"ask": "更压抑"})
    assert c == 200 and r["prompt"].startswith("中近景") and len(r["why"]) == 2
    sysmsg, user = seen[-1][0]["content"], seen[-1][1]["content"]
    assert "水墨" in sysmsg and "否掉 KF1：太亮" in sysmsg and "更压抑" in user and "教室" in user
    assert _req(base, "POST", "/api/director/revise/REF1", {})[0] == 400
    _, r = _req(base, "POST", "/api/director/review", {})
    assert r["summary"] == "基本连贯" and [i["known"] for i in r["issues"]] == [True, False]
    _, fb = _req(base, "GET", "/api/director/feedback")
    assert len(fb["items"]) == 1
    # AI 分镜也带导演设定
    monkeypatch.setattr(muse2api.Client, "chat", lambda self, m, **k: (seen.append(m), "SH01 | a | b | 3 |")[1])
    _req(base, "POST", "/api/storyboard/draft", {"story": "x"})
    assert "水墨" in seen[-1][0]["content"]
