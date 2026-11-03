"""内置“总导演/编导”：只用文字对话模型（muse-chat），不训练权重。

它懂什么：
- 工程里的 director/profile.md：设定集与镜头规矩，可以在画布里编辑；
- director/feedback.jsonl：你每次选定或否掉一张候选时留下的记录（附带理由），最近的若干条会作为“你的口味”交给模型。

它能做什么（都只出建议，不会自动改图、不会花钱）：
- revise(node)：按设定集和你的口味重写某个首帧或视频的提示词，并说明改了什么；
- review()：通读全片的提示词，指出前后镜头不连贯、违反设定集、画面里要出字之类的问题。

看图审片需要能读图的模型；当前 Muse 对话网关只传文字（NOT_VERIFIED，待实测），所以这里不传图片。
"""
from __future__ import annotations

import json
import os
import re
import time

DEFAULT_PROFILE = """# 导演设定（可以改）

## 画风
- 写这部片的统一画风：线条、色调、光线、质感。

## 角色与还原
- 角色外观以参考图为准，提示词里不重复描述外观，避免与参考图冲突。

## 镜头规矩
- 每个首帧写清景别、机位、构图、人物位置、光源方向。
- 视频提示词只写一个主要动作和一个运镜，避免多个动作挤在 5 秒里。
- 前后镜头的光线方向、时间、天气保持一致；换场时要明确写出来。

## 禁止
- 画面里不出现文字、字幕、水印、Logo。
"""

FEEDBACK_KEEP = 20


def _dir(project_dir: str) -> str:
    d = os.path.join(project_dir, "director")
    os.makedirs(d, exist_ok=True)
    return d


def load_profile(project_dir: str) -> str:
    p = os.path.join(_dir(project_dir), "profile.md")
    if not os.path.exists(p):
        return DEFAULT_PROFILE
    with open(p, encoding="utf-8") as f:
        return f.read()


def save_profile(project_dir: str, text: str):
    if len(text) > 20000:
        raise ValueError("导演设定太长（上限 2 万字）")
    with open(os.path.join(_dir(project_dir), "profile.md"), "w", encoding="utf-8") as f:
        f.write(text)


def add_feedback(project_dir: str, node: str, verdict: str, prompt: str = "", file: str = "", note: str = ""):
    if verdict not in ("pick", "reject"):
        raise ValueError("verdict 只能是 pick 或 reject")
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "node": node, "verdict": verdict,
           "prompt": prompt[:1000], "file": file, "note": note[:500]}
    with open(os.path.join(_dir(project_dir), "feedback.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def recent_feedback(project_dir: str, n: int = FEEDBACK_KEEP) -> list[dict]:
    p = os.path.join(_dir(project_dir), "feedback.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out[-n:]


def _taste(fb: list[dict]) -> str:
    lines = []
    for r in fb:
        tag = "选定" if r["verdict"] == "pick" else "否掉"
        s = f"- {tag} {r['node']}"
        if r.get("note"):
            s += f"：{r['note']}"
        if r.get("prompt"):
            s += f"（当时提示词：{r['prompt'][:160]}）"
        lines.append(s)
    return "\n".join(lines) or "（还没有记录）"


SYSTEM = """你是这部动画短片的总导演兼编导。严格遵守下面的导演设定，并参考出品人过去的取舍记录（那代表他的口味）。
你只给文字建议，不评价你看不到的图片。回答必须是 JSON，不要代码块，不要多余文字。

【导演设定】
{profile}

【出品人的取舍记录（越靠后越新）】
{taste}"""


def _system(project_dir: str) -> dict:
    return {"role": "system", "content": SYSTEM.format(profile=load_profile(project_dir),
                                                       taste=_taste(recent_feedback(project_dir)))}


def _shots(g) -> list[dict]:
    out = []
    for nid in g.topo():
        n = g.nodes()[nid]
        if n["type"] in ("keyframe", "video") and (n.get("params") or {}).get("prompt"):
            out.append({"node": nid, "type": "首帧" if n["type"] == "keyframe" else "视频",
                        "label": n.get("label", ""), "prompt": n["params"]["prompt"]})
    return out


def parse_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise ValueError("导演的回答不是 JSON，可以再点一次")


def revise_messages(g, nid: str, ask: str = "") -> list:
    n = g.nodes().get(nid)
    if not n or n["type"] not in ("keyframe", "video"):
        raise ValueError("只能改首帧或视频节点的提示词")
    shots = _shots(g)
    idx = next((i for i, s in enumerate(shots) if s["node"] == nid), None)
    ctx = shots[max(0, (idx or 0) - 2):(idx or 0) + 3] if idx is not None else shots[:4]
    kind = "首帧（静态画面）" if n["type"] == "keyframe" else "视频（5 秒动作与运镜）"
    user = (f"请重写节点 {nid}（{n.get('label', '')}）的{kind}提示词。\n"
            f"当前提示词：{(n.get('params') or {}).get('prompt', '') or '（空）'}\n"
            f"前后镜头：{json.dumps(ctx, ensure_ascii=False)}\n"
            + (f"出品人的额外要求：{ask}\n" if ask else "")
            + '输出 JSON：{"prompt": "新提示词", "why": ["改动理由1", "改动理由2"]}')
    return [_system(g.dir), {"role": "user", "content": user}]


def review_messages(g) -> list:
    shots = _shots(g)
    if not shots:
        raise ValueError("画布上还没有带提示词的首帧或视频")
    user = ("按顺序通读全片的提示词，找出问题：前后不连贯（光线、时间、方位、动作衔接）、违反导演设定、"
            "一个镜头塞了太多动作、画面里要出文字、描述含糊。没有问题就返回空列表。\n"
            f"镜头：{json.dumps(shots, ensure_ascii=False)}\n"
            '输出 JSON：{"issues": [{"node": "节点id", "problem": "问题", "fix": "建议改法"}], "summary": "一句话总评"}')
    return [_system(g.dir), {"role": "user", "content": user}]


def revise(g, nid: str, chat, ask: str = "") -> dict:
    j = parse_json(chat(revise_messages(g, nid, ask)))
    if not isinstance(j, dict) or not str(j.get("prompt", "")).strip():
        raise ValueError("导演没给出新提示词，可以再点一次")
    why = j.get("why") or []
    return {"node": nid, "prompt": str(j["prompt"]).strip(),
            "why": [str(x) for x in (why if isinstance(why, list) else [why])][:6]}


def review(g, chat) -> dict:
    j = parse_json(chat(review_messages(g)))
    nodes = g.nodes()
    issues = [i for i in (j.get("issues") or []) if isinstance(i, dict)] if isinstance(j, dict) else []
    for i in issues:
        i["known"] = i.get("node") in nodes
    return {"issues": issues[:30], "summary": str((j or {}).get("summary", ""))[:300]}
