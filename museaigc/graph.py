"""多镜头画布：节点图 + 逐节点执行器。

思路：Muse 视频只吃 1 张首帧，所以多参考放在"首帧"这一步（1–4 张参考图 → 1 张首帧），
连续动作用"尾帧"把上一镜的最后一帧接成下一镜的首帧，切镜则各自从同一组参考出首帧。

节点类型（输入端口 → 输出）：
  ref        无输入                         → image   参考图（定妆/场景/设定集）
  keyframe   refs: 1–4 张 image             → image   首帧；由人在外部生成后 pick 选定
  video      frame: 恰好 1 张 image          → video   Muse 出片（一个 Take 只发一次）或 pick 导入
  tailframe  video: 恰好 1 段 video          → image   ffmpeg 抽尾帧
  trim       video: 恰好 1 段 video          → video   ffmpeg 裁切 in_s/dur_s
  concat     clips: ≥1 段 video（按连线顺序） → video   ffmpeg 拼接成片
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from .shotcard import sha256_file

SPEC = {
    "ref":       {"in": {}, "out": "image"},
    "keyframe":  {"in": {"refs": ("image", 1, 4)}, "out": "image"},
    "video":     {"in": {"frame": ("image", 1, 1)}, "out": "video"},
    "tailframe": {"in": {"video": ("video", 1, 1)}, "out": "image"},
    "trim":      {"in": {"video": ("video", 1, 1)}, "out": "video"},
    "concat":    {"in": {"clips": ("video", 1, 99)}, "out": "video"},
}
LOCAL_TYPES = {"tailframe", "trim", "concat"}   # 本机 ffmpeg，不耗 Muse 次数
MAX_TAIL_CHAIN = 3                               # 尾帧连续接力上限（漂移会累积）


class GraphError(Exception):
    pass


@dataclass
class Graph:
    path: str
    data: dict

    @classmethod
    def load(cls, path: str) -> "Graph":
        with open(path, encoding="utf-8") as f:
            return cls(os.path.abspath(path), json.load(f))

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @property
    def dir(self) -> str:
        return os.path.dirname(self.path)

    @property
    def work(self) -> str:
        return os.path.join(self.dir, ".museaigc")

    def nodes(self) -> dict:
        return {n["id"]: n for n in self.data.get("nodes", [])}

    def edges(self) -> list:
        return self.data.get("edges", [])

    def inputs(self, nid: str, port: str | None = None) -> list[str]:
        return [e["from"] for e in self.edges()
                if e["to"] == nid and (port is None or e.get("port") == port)]

    def abspath(self, p: str) -> str:
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.dir, p))

    # ---------- 校验 ----------
    def validate(self) -> list[str]:
        """返回警告；有错误直接抛 GraphError。"""
        nodes, errs, warns = self.nodes(), [], []
        if len(nodes) != len(self.data.get("nodes", [])):
            errs.append("节点 id 重复")
        for nid, n in nodes.items():
            if n.get("type") not in SPEC:
                errs.append(f"{nid}: 未知类型 {n.get('type')}")
        for e in self.edges():
            a, b, port = e.get("from"), e.get("to"), e.get("port")
            if a not in nodes or b not in nodes:
                errs.append(f"连线 {a}→{b}: 节点不存在"); continue
            spec_in = SPEC.get(nodes[b].get("type"), {}).get("in", {})
            if port not in spec_in:
                errs.append(f"连线 {a}→{b}: {nodes[b]['type']} 没有端口 {port}"); continue
            want = spec_in[port][0]
            got = SPEC.get(nodes[a].get("type"), {}).get("out")
            if got != want:
                errs.append(f"连线 {a}→{b}.{port}: 需要 {want}，给的是 {got}")
        for nid, n in nodes.items():
            for port, (_, lo, hi) in SPEC.get(n.get("type"), {}).get("in", {}).items():
                k = len(self.inputs(nid, port))
                if not lo <= k <= hi:
                    errs.append(f"{nid}.{port}: 需要 {lo}–{hi} 个输入，现有 {k}")
        try:
            order = self.topo()
        except GraphError as e:
            errs.append(str(e)); order = []
        if errs:
            raise GraphError("；".join(errs))
        depth = {}
        for nid in order:   # 尾帧链长度：video←tailframe←video←tailframe…
            n = nodes[nid]
            if n["type"] == "video":
                src = self.inputs(nid, "frame")[0]
                depth[nid] = depth.get(self.inputs(src, "video")[0], 0) + 1 \
                    if nodes[src]["type"] == "tailframe" else 0
                if depth[nid] >= MAX_TAIL_CHAIN:
                    warns.append(f"{nid}: 尾帧已连续接力 {depth[nid]} 次，建议回到参考节点重新出首帧")
        return warns

    def topo(self) -> list[str]:
        nodes = self.nodes()
        indeg = {n: 0 for n in nodes}
        for e in self.edges():
            if e.get("to") in indeg and e.get("from") in nodes:
                indeg[e["to"]] += 1
        ready = [n for n, d in indeg.items() if d == 0]
        out = []
        while ready:
            n = ready.pop(0); out.append(n)
            for e in self.edges():
                if e.get("from") == n and e.get("to") in indeg:
                    indeg[e["to"]] -= 1
                    if indeg[e["to"]] == 0:
                        ready.append(e["to"])
        if len(out) != len(nodes):
            raise GraphError("连线成环")
        return out

    # ---------- 状态 ----------
    def _state_path(self):
        return os.path.join(self.work, "state.json")

    def state(self) -> dict:
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _put_state(self, nid: str, rec: dict):
        st = self.state(); st[nid] = rec
        os.makedirs(self.work, exist_ok=True)
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path())

    def output(self, nid: str) -> dict | None:
        """节点当前有效输出（缓存键与上游一致才算有效）。"""
        rec = self.state().get(nid)
        if not rec or rec.get("key") != self.key(nid):
            return None
        p = self.abspath(rec["file"])
        if not os.path.isfile(p) or sha256_file(p) != rec["sha256"]:
            return None
        return rec

    def key(self, nid: str) -> str | None:
        """缓存键 = 类型 + 参数（不含位置/标签/pick）+ 各输入的 SHA。任一上游缺输出 → None。"""
        n = self.nodes()[nid]
        params = {k: v for k, v in (n.get("params") or {}).items() if k not in ("picked",)}
        ins = []
        for e in self.edges():
            if e["to"] == nid:
                up = self.output(e["from"])
                if up is None:
                    return None
                ins.append([e.get("port"), up["sha256"]])
        if n["type"] == "ref":
            f = self.abspath(params.get("file", ""))
            ins.append(["file", sha256_file(f) if os.path.isfile(f) else None])
        raw = json.dumps([n["type"], params, ins], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def status(self) -> dict:
        out = {}
        for nid in self.topo():
            n = self.nodes()[nid]
            if self.output(nid):
                out[nid] = "DONE"
            elif self.key(nid) is None:
                out[nid] = "BLOCKED"            # 上游未完成
            elif n["type"] == "keyframe" or (n["type"] == "video" and not (n.get("params") or {}).get("sendAllowed")):
                out[nid] = "WAITING_PICK" if n["type"] == "keyframe" else "WAITING_SEND"
            else:
                out[nid] = "READY"
        return out

    # ---------- 执行 ----------
    def _bind(self, nid: str, file: str, **extra) -> dict:
        p = self.abspath(file)
        rec = {"file": os.path.relpath(p, self.dir), "sha256": sha256_file(p),
               "bytes": os.path.getsize(p), "key": self.key(nid), **extra}
        self._put_state(nid, rec)
        return rec

    def _out_path(self, nid: str, ext: str) -> str:
        d = os.path.join(self.work, "out"); os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{nid}{ext}")

    def pick(self, nid: str, file: str) -> dict:
        """人工选定：首帧（keyframe）或导入已有成片（video）。"""
        n = self.nodes().get(nid)
        if not n or n["type"] not in ("keyframe", "video"):
            raise GraphError(f"{nid}: 只有 keyframe/video 节点能 pick")
        if self.key(nid) is None:
            raise GraphError(f"{nid}: 上游未完成，不能 pick")
        p = self.abspath(file)
        if not os.path.isfile(p):
            raise GraphError(f"文件不存在：{p}")
        return self._bind(nid, p, picked=True)

    def brief(self, nid: str) -> dict:
        """首帧/视频节点的生成说明：给生图或出片的人/Agent 用。"""
        n = self.nodes()[nid]
        refs = [{"node": u, **(self.output(u) or {})} for u in self.inputs(nid)]
        return {"node": nid, "type": n["type"], "label": n.get("label"),
                "prompt": (n.get("params") or {}).get("prompt", ""), "inputs": refs}

    # ---------- muse2api 生成 ----------
    def candidates(self, nid: str, tasks=None) -> list[dict]:
        from .muse2api import TaskRunner
        tasks = tasks or TaskRunner(self)
        k = self.key(nid)
        return [r for r in tasks.records(nid) if r["state"] == "DONE" and r["key"] == k]

    def generate(self, nid: str, tasks=None, send: bool = False, new_attempt: bool = False) -> dict:
        """经 muse2api 生成：keyframe → 一张候选（需 pick）；video → 直接绑定为输出。"""
        from .muse2api import TaskRunner
        self.validate()
        n = self.nodes().get(nid)
        if not n or n["type"] not in ("keyframe", "video"):
            raise GraphError(f"{nid}: 只有 keyframe/video 节点能生成")
        prm = n.get("params") or {}
        if prm.get("provider") != "muse2api":
            raise GraphError(f"{nid}: 节点参数 provider 不是 muse2api")
        key = self.key(nid)
        if key is None:
            raise GraphError(f"{nid}: 上游未完成")
        if not prm.get("prompt"):
            raise GraphError(f"{nid}: 缺少 prompt")
        tasks = tasks or TaskRunner(self)
        ins = [self.abspath(self.output(u)["file"]) for u in self.inputs(nid)]
        if n["type"] == "keyframe":
            rec = tasks.start_or_resume(nid, "image", lambda idem: tasks.client.submit_image(
                prm["prompt"], ins, prm.get("size", "16:9"), idem), key, new_attempt)
            return {**rec, "next": f"museaigc graph pick <图> {nid} {rec['file']}"}
        if not send or not prm.get("sendAllowed"):
            raise GraphError(f"{nid}: 视频生成需要节点 sendAllowed=true 且带 --send")
        cached = self.output(nid)
        if cached:
            return {**cached, "cached": True}
        rec = tasks.start_or_resume(nid, "video", lambda idem: tasks.client.submit_video(
            prm["prompt"], ins[0], int(prm.get("durationRequestSeconds", 5)), prm.get("aspect", "16:9"), idem),
            key, new_attempt)
        return self._bind(nid, rec["file"], take=rec["taskId"], provider="muse2api", state="RECEIPT_BOUND")

    def run(self, nid: str, video_runner=None, send: bool = False) -> dict:
        nodes = self.nodes()
        if nid not in nodes:
            raise GraphError(f"没有节点 {nid}")
        self.validate()
        n, prm = nodes[nid], nodes[nid].get("params") or {}
        if self.key(nid) is None:
            raise GraphError(f"{nid}: 上游未完成（{', '.join(self.inputs(nid))}）")
        cached = self.output(nid)
        if cached:
            return {**cached, "cached": True}
        t = n["type"]
        if t == "ref":
            f = self.abspath(prm.get("file", ""))
            if not os.path.isfile(f):
                raise GraphError(f"{nid}: 参考图不存在 {f}")
            if prm.get("sha256") and prm["sha256"].lower() != sha256_file(f):
                raise GraphError(f"{nid}: 参考图 SHA 与节点记录不符")
            return self._bind(nid, f)
        if t == "keyframe":
            raise GraphError(f"{nid}: 首帧需在外部按 brief 生成后执行 pick")
        if t == "video":
            if video_runner is None:
                raise GraphError(f"{nid}: 视频节点需通过 `museaigc graph run --send` 连接浏览器执行")
            frame = self.output(self.inputs(nid, "frame")[0])
            res = video_runner(self, nid, self.abspath(frame["file"]), frame["sha256"], prm, send)
            if res.get("state") != "RECEIPT_BOUND":
                return res
            return self._bind(nid, res["path"], take=res.get("take"), state="RECEIPT_BOUND")
        src = [self.abspath(self.output(u)["file"]) for u in self.inputs(nid)]
        if t == "tailframe":
            out = self._out_path(nid, ".png")
            back = float(prm.get("from_end_s", 0.1))
            _ffmpeg(["-sseof", f"-{back}", "-i", src[0], "-frames:v", "1", "-update", "1", out])
        elif t == "trim":
            out = self._out_path(nid, ".mp4")
            _ffmpeg(["-ss", str(prm.get("in_s", 0)), "-i", src[0], "-t", str(prm["dur_s"]),
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", out])
        elif t == "concat":
            out = self._out_path(nid, ".mp4")
            w, h, fps = prm.get("width", 1920), prm.get("height", 1080), prm.get("fps", 24)
            args, chains = [], []
            for i, s in enumerate(src):
                args += ["-i", s]
                chains.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1[v{i}]")
            fc = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(src))) + \
                f"concat=n={len(src)}:v=1:a=0[out]"
            _ffmpeg(args + ["-filter_complex", fc, "-map", "[out]", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", out])
        else:
            raise GraphError(f"{nid}: 不支持的类型 {t}")
        return self._bind(nid, out)


def _ffmpeg(args: list[str]):
    if not shutil.which("ffmpeg"):
        raise GraphError("找不到 ffmpeg，请先安装并加入 PATH")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GraphError("ffmpeg 失败：" + r.stderr[-400:])
