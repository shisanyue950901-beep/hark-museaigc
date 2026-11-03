"""分镜表 → 画布节点。

每行一个镜头，用 | 或制表符分隔：
    镜号 | 首帧画面 | 视频动作 | 秒数 | 接续
- 接续：写“接”= 用上一镜尾帧作首帧（动作连续）；写“切”或留空 = 从参考图重新出首帧。
- 秒数：成片里保留的时长（裁切），生成仍按 5 秒档请求。
- 以 # 开头的行、空行、表头行（镜号/shot）会被跳过。

展开结果：每镜 [首帧 或 尾帧] → 视频 → 裁切，最后全部接到一个拼接节点。
参考图节点：用画布上已有的 ref 节点（传入 ref_ids），每个首帧都连上这些参考（最多 4 张）。
尾帧接力连续超过 MAX_TAIL_CHAIN 次时，自动改为“切”（回到参考重出首帧），避免漂移累积。
"""
from __future__ import annotations

import re

from .graph import MAX_TAIL_CHAIN

GEN_SECONDS = 5


class StoryboardError(Exception):
    pass


def parse(text: str) -> list[dict]:
    rows = []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = [c.strip() for c in re.split(r"\t|\|", line)]
        if line.startswith("|"):
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if not cells or cells[0].lower() in ("镜号", "shot", "no", "#") or set(cells[0]) <= set("-: "):
            continue
        if len(cells) < 3:
            raise StoryboardError(f"第 {ln} 行至少要有 镜号 | 首帧画面 | 视频动作")
        shot = re.sub(r"[^\w\-]", "", cells[0]) or f"S{len(rows) + 1:02d}"
        try:
            dur = float(cells[3]) if len(cells) > 3 and cells[3] else 3.0
        except ValueError:
            raise StoryboardError(f"第 {ln} 行秒数不是数字：{cells[3]}") from None
        if not 0.5 <= dur <= GEN_SECONDS:
            raise StoryboardError(f"第 {ln} 行秒数需在 0.5–{GEN_SECONDS} 之间")
        cont = len(cells) > 4 and cells[4] in ("接", "续", "continue", "cont")
        rows.append({"shot": shot, "frame": cells[1], "action": cells[2], "dur": dur, "cont": cont})
    if not rows:
        raise StoryboardError("分镜表是空的")
    if rows[0]["cont"]:
        raise StoryboardError("第一镜不能写“接”：前面没有镜头")
    shots = [r["shot"] for r in rows]
    if len(set(shots)) != len(shots):
        raise StoryboardError("镜号有重复")
    return rows


def expand(data: dict, rows: list[dict], ref_ids: list[str], x0: int = 60, y0: int = 420,
           aspect: str = "16:9") -> dict:
    """把分镜行追加到图数据里，返回 {"added": [...], "concat": id, "notes": [...]}。"""
    nodes = {n["id"]: n for n in data.setdefault("nodes", [])}
    edges = data.setdefault("edges", [])
    refs = [r for r in ref_ids if nodes.get(r, {}).get("type") == "ref"][:4]
    if not refs:
        raise StoryboardError("先在画布上放至少 1 个参考图节点（最多用 4 个）")
    added, notes, prev_video, chain = [], [], None, 0

    def uid(base):
        i, nid = 1, base
        while nid in nodes:
            i += 1; nid = f"{base}_{i}"
        return nid

    def add(nid, typ, label, x, y, params):
        n = {"id": nid, "type": typ, "label": label, "x": x, "y": y, "params": params}
        data["nodes"].append(n); nodes[nid] = n; added.append(nid)
        return nid

    clips = []
    for i, r in enumerate(rows):
        y = y0 + i * 230
        s = r["shot"]
        cont = r["cont"] and prev_video is not None
        if cont and chain + 1 >= MAX_TAIL_CHAIN:
            notes.append(f"{s}: 尾帧已连续接力 {chain + 1} 次，改为从参考图重出首帧")
            cont = False
        if cont:
            src = add(uid(f"{s}_tail"), "tailframe", f"{s} 接上一镜尾帧", x0, y, {"from_end_s": 0.1})
            edges.append({"from": prev_video, "to": src, "port": "video"})
            chain += 1
        else:
            src = add(uid(f"{s}_kf"), "keyframe", f"{s} 首帧", x0, y,
                      {"provider": "muse-open", "size": aspect, "prompt": r["frame"]})
            for ref in refs:
                edges.append({"from": ref, "to": src, "port": "refs"})
            chain = 0
        v = add(uid(f"{s}_v"), "video", f"{s} 视频", x0 + 250, y,
                {"provider": "muse-open", "aspect": aspect, "durationRequestSeconds": GEN_SECONDS,
                 "sendAllowed": True, "prompt": r["action"]})
        edges.append({"from": src, "to": v, "port": "frame"})
        t = add(uid(f"{s}_cut"), "trim", f"{s} 裁 {r['dur']:g} 秒", x0 + 500, y,
                {"in_s": 0, "dur_s": r["dur"]})
        edges.append({"from": v, "to": t, "port": "video"})
        clips.append(t)
        prev_video = v
    w, h = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:3": (1440, 1080), "3:4": (1080, 1440)}.get(aspect, (1920, 1080))
    c = add(uid("FINAL"), "concat", "成片拼接", x0 + 760, y0, {"width": w, "height": h, "fps": 24})
    for t in clips:
        edges.append({"from": t, "to": c, "port": "clips"})
    return {"added": added, "concat": c, "notes": notes,
            "seconds": round(sum(r["dur"] for r in rows), 2)}


DRAFT_SYSTEM = """你是动画分镜师。把用户的故事拆成镜头表，只输出表格行，不要解释、不要代码块。
每行格式：镜号 | 首帧画面 | 视频动作 | 成片秒数 | 接续
规则：
- 镜号用 SH01、SH02…；成片秒数 1–5，常用 2–3；
- 首帧画面写景别、构图、人物位置、光线，不写人物外观细节（外观由参考图决定）；
- 视频动作只写动作和运镜，一句话；
- 动作和上一镜连续时“接续”写“接”，首帧画面可留空；换场景或换景别时留空；不要连续 3 次以上写“接”；
- 画面内不要出现文字、字幕、水印。"""


def draft_request(story: str, shots: int | None = None) -> list:
    want = f"大约 {shots} 个镜头。" if shots else ""
    return [{"role": "system", "content": DRAFT_SYSTEM},
            {"role": "user", "content": f"{want}故事：\n{story.strip()}"}]


def clean_draft(text: str) -> str:
    """只保留像镜头行的内容（模型偶尔会加说明或代码块）。"""
    keep = []
    for ln in text.splitlines():
        t = ln.strip().strip("`")
        if t.count("|") >= 2 and not set(t.replace("|", "").strip()) <= set("-: "):
            keep.append(t)
    return "\n".join(keep)
