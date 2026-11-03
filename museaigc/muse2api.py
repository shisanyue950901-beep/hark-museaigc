"""muse2api 提供方：通过用户自己部署的 muse2api（OpenAI 兼容）出图、出视频。

接口（来自 muse2api README，第三方，NOT_VERIFIED 于真实实例）：
  POST /v1/images/tasks   {model,prompt,size,images:[{url}]}  + Idempotency-Key → 202 {id}
  GET  /v1/images/tasks/{id}  → status: queued|running|completed|failed, url | data[0].url, error
  POST /v1/videos         {prompt,duration,size,images:[{url}]}  → {id,status}
  GET  /v1/videos/{id}    → status: queued|...|succeeded|failed, result.url, error

纪律：
  - 每次提交先写任务记录（含幂等键），提交后写 task id；再次运行只续轮询，绝不重新 POST。
  - 轮询失败只重试轮询；任务 failed 即终态，要重来必须显式 --new-attempt。
  - 结果下载到本地，记录字节与 SHA-256。
  - Key 只从环境变量 MUSE2API_KEY 或系统凭据库（keyring，服务名 museaigc / muse2api）读取，不写入图文件或日志。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .shotcard import sha256_file

DONE = {"completed", "succeeded", "success", "done"}
FAILED = {"failed", "error", "cancelled", "canceled"}


class Muse2APIError(Exception):
    pass


def api_key() -> str:
    k = os.environ.get("MUSE2API_KEY", "").strip()
    if k:
        return k
    try:
        import keyring  # 可选依赖：Windows 凭据管理器 / macOS 钥匙串
        k = (keyring.get_password("museaigc", "muse2api") or "").strip()
    except Exception:
        k = ""
    if not k:
        raise Muse2APIError("缺少 muse2api 密钥：设置环境变量 MUSE2API_KEY，"
                            "或 `python -m keyring set museaigc muse2api`")
    return k


def base_url() -> str:
    return os.environ.get("MUSE2API_BASE", "http://127.0.0.1:18610").rstrip("/")


def data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


class Client:
    def __init__(self, base: str | None = None, key: str | None = None, timeout: float = 60):
        self.base = (base or base_url()).rstrip("/")
        self.key = key or api_key()
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        url = path if path.startswith("http") else self.base + path
        data = json.dumps(body).encode() if body is not None else None
        h = {"Authorization": f"Bearer {self.key}", **(headers or {})}
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        host = urllib.parse.urlsplit(url).hostname or ""
        opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
                  if host in ("127.0.0.1", "localhost", "::1") else urllib.request.build_opener())
        try:
            with opener.open(req, timeout=self.timeout) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "")
                return r.status, (json.loads(raw) if "json" in ctype and raw else raw)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            raise Muse2APIError(f"HTTP {e.code} {path}: {msg}") from None
        except urllib.error.URLError as e:
            raise Muse2APIError(f"连不上 muse2api（{self.base}）：{e.reason}") from None

    def submit_image(self, prompt, refs, size, idem):
        body = {"model": "muse-image", "prompt": prompt, "size": size, "response_format": "url"}
        if refs:
            body["images"] = [{"url": data_url(p)} for p in refs]
        _, j = self._req("POST", "/v1/images/tasks", body, {"Idempotency-Key": idem})
        return j["id"]

    def submit_video(self, prompt, frame, duration, size, idem):
        body = {"prompt": prompt, "duration": duration, "size": size, "images": [{"url": data_url(frame)}]}
        _, j = self._req("POST", "/v1/videos", body, {"Idempotency-Key": idem})
        return j["id"]

    def poll(self, kind: str, tid: str) -> dict:
        _, j = self._req("GET", f"/v1/{'images/tasks' if kind == 'image' else 'videos'}/{tid}")
        return j

    def download(self, url: str, dest: str):
        _, raw = self._req("GET", url)
        if isinstance(raw, (dict, list)):
            raise Muse2APIError("媒体地址返回的是 JSON 而不是文件")
        with open(dest + ".part", "wb") as f:
            f.write(raw)
        os.replace(dest + ".part", dest)


def result_url(j: dict) -> str | None:
    if isinstance(j.get("result"), dict) and j["result"].get("url"):
        return j["result"]["url"]
    if j.get("url"):
        return j["url"]
    d = j.get("data")
    if isinstance(d, list) and d and d[0].get("url"):
        return d[0]["url"]
    return None


class TaskRunner:
    """一个节点一次尝试 = 一条任务记录（.museaigc/tasks/<node>.<n>.json）。"""

    def __init__(self, graph, client: Client | None = None, poll_s: float = 3, timeout_s: float = 900):
        self.g, self._client, self.poll_s, self.timeout_s = graph, client, poll_s, timeout_s

    @property
    def client(self):
        if self._client is None:
            self._client = Client()
        return self._client

    def _dir(self):
        d = os.path.join(self.g.work, "tasks"); os.makedirs(d, exist_ok=True); return d

    def records(self, nid: str) -> list[dict]:
        out = []
        for f in sorted(os.listdir(self._dir())):
            if f.startswith(nid + ".") and f.endswith(".json"):
                with open(os.path.join(self._dir(), f), encoding="utf-8") as fh:
                    out.append(json.load(fh))
        return sorted(out, key=lambda r: r["attempt"])

    def _save(self, rec: dict):
        p = os.path.join(self._dir(), f"{rec['node']}.{rec['attempt']:03d}.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(p + ".tmp", p)

    def start_or_resume(self, nid: str, kind: str, submit, key: str, new_attempt: bool = False) -> dict:
        recs = self.records(nid)
        last = recs[-1] if recs else None
        if last and last["key"] == key and not new_attempt:
            if last["state"] in ("SUBMITTING",):
                # 提交结果不明：不补发，交给人判断
                raise Muse2APIError(f"{nid}: 上次提交结果不明（无 task id），为避免重复出片不再自动提交；"
                                    "请在 muse2api 面板核对后用 --new-attempt 重来")
            if last["state"] in ("SUBMITTED", "DONE"):
                return self._wait(last, kind)
            if last["state"] == "FAILED":
                raise Muse2APIError(f"{nid}: 任务 {last.get('taskId')} 已失败：{last.get('error')}；"
                                    "要重来请加 --new-attempt")
        rec = {"node": nid, "kind": kind, "attempt": (last["attempt"] + 1 if last else 1),
               "key": key, "idempotencyKey": str(uuid.uuid4()), "state": "SUBMITTING",
               "createdAt": time.time()}
        self._save(rec)
        rec["taskId"] = submit(rec["idempotencyKey"])
        rec["state"] = "SUBMITTED"
        self._save(rec)
        return self._wait(rec, kind)

    def _wait(self, rec: dict, kind: str) -> dict:
        if rec["state"] == "DONE":
            return rec
        deadline = time.monotonic() + self.timeout_s
        last_err = None
        while time.monotonic() < deadline:
            try:
                j = self.client.poll(kind, rec["taskId"])
            except Muse2APIError as e:      # 只重试查询
                last_err = str(e); time.sleep(self.poll_s); continue
            st = str(j.get("status", "")).lower()
            rec["status"] = st
            if st in DONE:
                url = result_url(j)
                if not url:
                    rec.update(state="FAILED", error="完成但没有结果地址"); self._save(rec)
                    raise Muse2APIError(f"{rec['node']}: 完成但没有结果地址")
                ext = os.path.splitext(url.split("?")[0])[1] or (".mp4" if kind == "video" else ".png")
                d = os.path.join(self.g.work, "out", rec["node"]); os.makedirs(d, exist_ok=True)
                dest = os.path.join(d, f"attempt{rec['attempt']:03d}{ext}")
                self.client.download(url, dest)
                rec.update(state="DONE", file=os.path.relpath(dest, self.g.dir),
                           sha256=sha256_file(dest), bytes=os.path.getsize(dest), doneAt=time.time())
                self._save(rec)
                return rec
            if st in FAILED:
                rec.update(state="FAILED", error=str(j.get("error") or st)[:300]); self._save(rec)
                raise Muse2APIError(f"{rec['node']}: 任务失败：{rec['error']}")
            time.sleep(self.poll_s)
        self._save(rec)
        raise Muse2APIError(f"{rec['node']}: 等待超时（任务 {rec['taskId']} 仍在进行，"
                            f"再次运行会继续轮询，不会重发）{'；' + last_err if last_err else ''}")
