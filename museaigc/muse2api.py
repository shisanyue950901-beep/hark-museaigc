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
FAILED = {"failed", "error", "cancelled", "canceled", "interrupted", "timeout", "timed_out", "expired", "aborted"}


FRIENDLY = {
    "no_healthy_account": "服务端暂时没有可用的 Muse 账号，任务没有提交，稍后再试",
    "too_many_running": "同时进行的任务太多，等前面的完成再提交",
    "text_upstream_unauthorized": "Muse 文本上游的凭据失效了（不是你的 key），需要服务端管理员更新",
    "chat_timeout": "对话超时（服务端上限 90 秒），可以把要求写短一点再试",
    "task_not_found": "服务端没有这个任务",
    "invalid_api_key": "Key 不对或已停用",
}


class Muse2APIError(Exception):
    def __init__(self, msg, status: int | None = None, code: str | None = None):
        self.code = code
        super().__init__(msg); self.status = status


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


def _multipart(fields: dict, files: list[tuple[str, str]]):
    """fields: 文本字段；files: [(字段名, 本地路径)]。返回 (body, content_type)。"""
    bd = "----museaigc" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out += [f"--{bd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode(),
                str(v).encode(), b"\r\n"]
    for k, path in files:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        out += [f"--{bd}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: {mime}\r\n\r\n".encode(),
                data, b"\r\n"]
    out.append(f"--{bd}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={bd}"


class Client:
    """api="openai"：/v1/images/tasks、/v1/videos（muse2api 风格）。
    api="open"：/api/open/v1/*（产品 API，异步任务，pending→processing→done|failed）。"""

    def __init__(self, base: str | None = None, key: str | None = None, timeout: float = 60,
                 api: str | None = None):
        self.base = (base or base_url()).rstrip("/")
        self.key = key or api_key()
        self.timeout = timeout
        self.api = api or os.environ.get("MUSE2API_API", "openai")

    def _req(self, method: str, path: str, body=None, headers: dict | None = None):
        url = path if path.startswith("http") else self.base + path
        h = {"Authorization": f"Bearer {self.key}", **(headers or {})}
        if isinstance(body, tuple):          # 预编码的 multipart
            data, h["Content-Type"] = body
        elif body is not None:
            data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        else:
            data = None
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
            raw = e.read().decode(errors="replace")
            code, text = None, raw[:300]
            try:
                err = json.loads(raw).get("error") or {}
                if isinstance(err, dict):
                    code, text = err.get("code"), err.get("message") or text
            except (ValueError, AttributeError):
                if "<html" in raw[:200].lower():
                    text = "网关返回了网页（多半是隧道超时或服务端崩溃）"
            hint = FRIENDLY.get(code)
            ra = e.headers.get("Retry-After") if e.headers else None
            msg = f"HTTP {e.code} {path}: {hint or text}" + (f"（{ra} 秒后再试）" if ra else "")
            raise Muse2APIError(msg, e.code, code) from None
        except urllib.error.URLError as e:
            raise Muse2APIError(f"连不上 muse2api（{self.base}）：{e.reason}") from None

    # 产品 API（MuseStudio ≥ 11-04）支持幂等键补交与补取/取消
    @property
    def idempotent(self) -> bool:
        return self.api == "open"

    @property
    def can_recover(self) -> bool:
        return self.api == "open"

    def recover(self, tid: str):
        try:
            return self._req("POST", f"/api/open/v1/tasks/{tid}/recover")
        except Muse2APIError as e:
            if e.status == 409:
                return 409, {"reason": e.code or str(e)}
            raise

    def cancel(self, tid: str) -> dict:
        _, j = self._req("POST", f"/api/open/v1/tasks/{tid}/cancel")
        return j if isinstance(j, dict) else {}

    def chat(self, messages: list, model: str = "muse-chat", timeout: float = 100) -> str:
        """OpenAI 兼容对话（/v1/chat/completions，非流式）。"""
        old, self.timeout = self.timeout, timeout
        try:
            _, j = self._req("POST", "/v1/chat/completions",
                             {"model": model, "messages": messages, "stream": False})
        finally:
            self.timeout = old
        try:
            return j["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise Muse2APIError(f"对话返回格式不对：{str(j)[:200]}") from None

    def check_quota(self):
        """产品 API：提交前查额度，用完就不提交。"""
        if self.api != "open":
            return None
        _, j = self._req("GET", "/api/open/v1/points")
        if j.get("daily_quota") is not None and j.get("today_used", 0) >= j["daily_quota"]:
            raise Muse2APIError(f"今日额度已用完（{j['today_used']}/{j['daily_quota']}），不提交")
        if j.get("points") is not None and j["points"] <= 0:
            raise Muse2APIError("积分为 0，不提交")
        return j

    def _open_submit(self, path_json, path_mp, prompt, refs, extra, idem):
        hdr = {"Idempotency-Key": idem}
        if refs:
            fields = {"prompt": prompt, **{k: v for k, v in extra.items() if v is not None}}
            _, j = self._req("POST", path_mp, _multipart(fields, [("image", p) for p in refs]), hdr)
        else:
            _, j = self._req("POST", path_json, {"prompt": prompt, **extra}, hdr)
        return j.get("task_id") or j["id"]

    def submit_image(self, prompt, refs, size, idem):
        if self.api == "open":
            return self._open_submit("/api/open/v1/images", "/api/open/v1/images/img2img",
                                     prompt, refs, {"size": size}, idem)
        body = {"model": "muse-image", "prompt": prompt, "size": size, "response_format": "url"}
        if refs:
            body["images"] = [{"url": data_url(p)} for p in refs]
        _, j = self._req("POST", "/v1/images/tasks", body, {"Idempotency-Key": idem})
        return j["id"]

    def submit_video(self, prompt, frame, duration, size, idem):
        if self.api == "open":
            return self._open_submit("/api/open/v1/videos", "/api/open/v1/videos/img2video",
                                     prompt, [frame], {"duration": duration, "size": size}, idem)
        body = {"prompt": prompt, "duration": duration, "size": size, "images": [{"url": data_url(frame)}]}
        _, j = self._req("POST", "/v1/videos", body, {"Idempotency-Key": idem})
        return j["id"]

    def poll(self, kind: str, tid: str) -> dict:
        if self.api == "open":
            _, j = self._req("GET", f"/api/open/v1/tasks/{tid}")
            return j
        _, j = self._req("GET", f"/v1/{'images/tasks' if kind == 'image' else 'videos'}/{tid}")
        return j

    def media_url(self, url: str) -> str:
        """服务端常把媒体地址写成它自己的 127.0.0.1；经隧道或局域网访问时改走 base。"""
        u = urllib.parse.urlsplit(url)
        if not u.scheme:
            return self.base + (url if url.startswith("/") else "/" + url)
        b = urllib.parse.urlsplit(self.base)
        if u.hostname in ("127.0.0.1", "localhost", "0.0.0.0", "::1") and u.netloc != b.netloc:
            return self.base + u.path + (("?" + u.query) if u.query else "")
        return url

    def download(self, url: str, dest: str):
        _, raw = self._req("GET", self.media_url(url))
        if isinstance(raw, (dict, list)):
            raise Muse2APIError("媒体地址返回的是 JSON 而不是文件")
        with open(dest + ".part", "wb") as f:
            f.write(raw)
        os.replace(dest + ".part", dest)


def result_url(j) -> str | None:
    """在 result / url / data / urls 里找第一个媒体地址（两种 API 的结果形状不同）。"""
    if isinstance(j, str):
        return j if (j.startswith("http") or j.startswith("/")) else None
    if isinstance(j, list):
        for x in j:
            u = result_url(x)
            if u:
                return u
        return None
    if not isinstance(j, dict):
        return None
    for k in ("url", "video_url", "image_url", "file_url"):
        if isinstance(j.get(k), str) and j[k]:
            return j[k]
    for k in ("result", "data", "urls", "images", "videos", "outputs", "files", "attachments", "media"):
        if k in j:
            u = result_url(j[k])
            if u:
                return u
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
            if last["state"] in ("SUBMITTING",) and getattr(self.client, "idempotent", False):
                # 服务端支持 Idempotency-Key（24 小时内同键同任务）：用原来的键补交，不会重复出片或扣分
                try:
                    last["taskId"] = submit(last["idempotencyKey"])
                except Muse2APIError as e:
                    if e.status and 400 <= e.status < 500 and e.status != 408:
                        last.update(state="FAILED", error=str(e)[:300]); self._save(last)
                    raise
                last["state"] = "SUBMITTED"; last["resubmittedSameKey"] = True; self._save(last)
                return self._wait(last, kind)
            if last["state"] in ("SUBMITTING",):
                # 提交结果不明：不补发，交给人判断
                raise Muse2APIError(f"{nid}: 上次提交结果不明（无 task id），为避免重复出片不再自动提交；"
                                    "请在 muse2api 面板核对后用 --new-attempt 重来")
            if last["state"] in ("SUBMITTED", "DONE"):
                return self._wait(last, kind)
            if last["state"] == "FAILED":
                raise Muse2APIError(f"{nid}: 任务 {last.get('taskId')} 已失败：{last.get('error')}；"
                                    "要重来请加 --new-attempt")
        self.client.check_quota()          # 额度不够就不建记录、不提交
        rec = {"node": nid, "kind": kind, "attempt": (last["attempt"] + 1 if last else 1),
               "key": key, "idempotencyKey": str(uuid.uuid4()), "state": "SUBMITTING",
               "createdAt": time.time()}
        self._save(rec)
        try:
            rec["taskId"] = submit(rec["idempotencyKey"])
        except Muse2APIError as e:
            if (e.status and 400 <= e.status < 500 and e.status != 408) or e.code == "no_healthy_account":
                # 服务端明确拒收（含 503 no_healthy_account）：确定没有建任务，记为失败，可直接重来
                rec.update(state="FAILED", error=str(e)[:300], retryable=True); self._save(rec)
            raise                              # 其余（断线、5xx、超时）保持 SUBMITTING = 结果不明
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
            rec["stage"] = j.get("stage")            # 终态时服务端给 null，跟着清掉
            for k in ("upstream_id", "cost_points"):
                if j.get(k) is not None:
                    rec[k] = j[k]
            res = j.get("result") if isinstance(j.get("result"), dict) else {}
            att = next((a for a in res.get("attachments") or [] if isinstance(a, dict)), {})
            for k in ("width", "height"):              # 实际尺寸：result 或 attachments[0]
                v = res.get(k) or att.get(k) or j.get(k)
                if v:
                    rec[k] = v
            if st in DONE:
                url = result_url(j)
                if not url:
                    # 服务端已完成（可能已扣费）：保持 SUBMITTED，下次点“生成”只重新查询取回，不会重新提交
                    rec["lastError"] = "完成但没有找到结果地址"; self._save(rec)
                    raise Muse2APIError(f"{rec['node']}: 任务已完成但没找到结果地址；再点一次“生成”只会重新取回，不会重新提交")
                ext = os.path.splitext(url.split("?")[0])[1] or (".mp4" if kind == "video" else ".png")
                d = os.path.join(self.g.work, "out", rec["node"]); os.makedirs(d, exist_ok=True)
                dest = os.path.join(d, f"attempt{rec['attempt']:03d}{ext}")
                self.client.download(url, dest)
                rec.update(state="DONE", file=os.path.relpath(dest, self.g.dir),
                           sha256=sha256_file(dest), bytes=os.path.getsize(dest), doneAt=time.time())
                self._save(rec)
                return rec
            if st in FAILED and rec.get("upstream_id") and not rec.get("recoverTried") \
                    and getattr(self.client, "can_recover", False) and st not in ("canceled", "cancelled"):
                # 服务端判失败但上游可能已出片：拿上游任务号补取一次
                rec["recoverTried"] = True; self._save(rec)
                try:
                    code, rj = self.client.recover(rec["taskId"])
                except Muse2APIError as e:
                    rec["recoverError"] = str(e)[:200]
                else:
                    if code == 200 and rj.get("recovered"):
                        continue                     # 下一轮 poll 会看到 succeeded
                    if code == 202:
                        time.sleep(self.poll_s); continue
                    rec["recoverError"] = str(rj.get("reason") or rj)[:200]
            if st in FAILED:
                rec.update(state="FAILED", error=str(j.get("error") or st)[:300]); self._save(rec)
                raise Muse2APIError(f"{rec['node']}: 任务失败：{rec['error']}")
            time.sleep(self.poll_s)
        self._save(rec)
        raise Muse2APIError(f"{rec['node']}: 等待超时（任务 {rec['taskId']} 仍在进行，"
                            f"再次运行会继续轮询，不会重发）{'；' + last_err if last_err else ''}")
