"""Muse 网页驱动：上传首帧 → 写提示词 → 核对 → 点一次发送 → 等新视频附件 → 取回 MP4。

流程借鉴 muse2api（MIT）的 DOM 判定思路，代码为本项目独立实现，见 NOTICE.md。
回执标准：基线之外的新附件 + 真实视频源 + 已取回字节 + SHA。封面图、文字声称"已生成"都不算。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from .journal import Journal
from .selectors import Selectors
from .shotcard import ShotCard, sha256_text


class DriverError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code


@dataclass
class Timeouts:
    login_s: float = 30
    upload_confirm_s: float = 15
    send_observe_s: float = 120
    receipt_s: float = 15 * 60
    poll_s: float = 2.0
    text_only_grace_s: float = 30


@dataclass
class Result:
    state: str
    take_id: str
    data: dict = field(default_factory=dict)


_JS_PROBE = r"""
(s) => {
  const vis = (el) => !!el && el.offsetParent !== null;
  const ov = document.querySelector(s.composer_overlay);
  const composer = ov ? (ov.closest('form') || ov.parentElement) : null;
  const scope = composer || document;
  const ta = scope.querySelector(s.textarea) || document.querySelector(s.textarea);
  const sendRe = new RegExp(s.send_label_re, 'i');
  const btns = [...document.querySelectorAll('button,[role=button]')].filter(vis);
  const send = btns.find(b => sendRe.test(b.getAttribute('aria-label') || '') ||
                              sendRe.test(b.getAttribute('data-testid') || ''));
  const rmRe = new RegExp(s.remove_attachment_re, 'i');
  const removeCount = composer ? [...composer.querySelectorAll('button[aria-label]')]
      .filter(b => rmRe.test(b.getAttribute('aria-label') || '')).length : 0;
  const dialogs = [...document.querySelectorAll(s.dialog)].filter(vis).length;
  const quota = new RegExp(s.quota_re, 'i').test(document.body.innerText || '');
  return {
    composer: !!composer, textarea: !!ta, textValue: ta ? ta.value : null,
    send: send ? (send.disabled ? 'disabled' : 'enabled') : 'missing',
    stop: !!document.querySelector(s.stop_button),
    removeCount, dialogs, quota,
    fileInputs: scope.querySelectorAll(s.file_input).length,
    attachments: document.querySelectorAll(s.attachment).length,
    agentBubbles: document.querySelectorAll(s.agent_bubble).length,
  };
}
"""

_JS_ATTACHMENTS = r"""
(s) => [...document.querySelectorAll(s.attachment)].map((a, i) => {
  const v = a.querySelector('video');
  let vsrc = '';
  if (v) vsrc = v.currentSrc || v.src || (v.querySelector('source') || {}).src || '';
  const img = a.querySelector('img');
  return { idx: i, tid: a.getAttribute('data-testid') || '', vsrc,
           img: img ? img.src : '', w: v ? v.videoWidth : 0, h: v ? v.videoHeight : 0 };
})
"""

_JS_AGENT_TEXT = r"""
(s) => { const b = [...document.querySelectorAll(s.agent_bubble)];
         return b.length ? (b[b.length - 1].innerText || '') : ''; }
"""

_JS_FETCH_B64 = r"""
async (src) => {
  const r = await fetch(src);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const buf = new Uint8Array(await r.arrayBuffer());
  let s = ''; const CH = 0x8000;
  for (let i = 0; i < buf.length; i += CH) s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
  return { b64: btoa(s), type: r.headers.get('content-type') || '' };
}
"""

_VIDEO_EXT = re.compile(r"\.(mp4|webm|mov)(\?|#|$)", re.I)


def _redact_url(u: str) -> str:
    """去掉 query/fragment，避免签名参数落盘。"""
    return re.split(r"[?#]", u or "", maxsplit=1)[0]


def _is_video(att: dict) -> bool:
    v = att.get("vsrc") or ""
    return bool(v) and (v.startswith("blob:") or bool(_VIDEO_EXT.search(v)) or "video" in v.lower())


class MuseDriver:
    def __init__(self, page, selectors: Selectors | None = None,
                 timeouts: Timeouts | None = None, runs_dir: str = "runs", log=print):
        self.page = page
        self.s = selectors or Selectors()
        self.t = timeouts or Timeouts()
        self.runs_dir = runs_dir
        self.log = log

    # ---------- 只读 ----------
    def probe(self) -> dict:
        return self.page.evaluate(_JS_PROBE, self.s.to_dict())

    def attachments(self) -> list[dict]:
        return self.page.evaluate(_JS_ATTACHMENTS, self.s.to_dict())

    def doctor(self) -> dict:
        """只读体检：不输入、不上传、不点击。"""
        self._ensure_loaded()
        p = self.probe()
        p["url"] = _redact_url(self.page.url)
        p["ok"] = p["composer"] and p["textarea"] and p["fileInputs"] > 0
        return p

    def _ensure_loaded(self):
        if not self.page.url.startswith(self.s.base_url.rstrip("/")):
            self.page.goto(self.s.base_url, wait_until="domcontentloaded")
        deadline = time.monotonic() + self.t.login_s
        while time.monotonic() < deadline:
            try:
                if self.page.locator(self.s.composer_overlay).count() > 0:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise DriverError("NOT_READY", "找不到 Muse 输入区：可能未登录、页面改版或网络慢。先在该 Chrome 里登录 muse.ai。")

    # ---------- 发送 ----------
    def run(self, card: ShotCard, take_id: str, send: bool = False) -> Result:
        j = Journal(self.runs_dir, take_id)
        if j.ever_reserved():
            raise DriverError("ALREADY_SENT", f"Take {take_id} 已发送过；不补发，请新开 Take")
        if send and not card.send_allowed:
            raise DriverError("SEND_NOT_ALLOWED", "镜头卡 sendAllowed=false，只能 --dry-run")

        frame_sha = card.verify_first_frame()
        prompt = card.final_prompt()
        prompt_sha = sha256_text(prompt)
        j.write("PREPARED", shot=card.shot_id, firstFrameSHA256=frame_sha,
                promptSHA256=prompt_sha, send=send)

        try:
            self._ensure_loaded()
            self.page.bring_to_front()
            p = self.probe()
            if p["removeCount"] or (p["textValue"] or "").strip():
                raise DriverError("COMPOSER_NOT_EMPTY", "输入区已有内容或附件，请先清空再运行")
            if p["quota"]:
                raise DriverError("QUOTA", "页面显示额度用尽/需升级")

            baseline = self.attachments()
            base_keys = {(a["tid"], a["vsrc"], a["img"]) for a in baseline}
            base_vsrc = {a["vsrc"] for a in baseline if a["vsrc"]}
            base_agent = p["agentBubbles"]

            if card.first_frame:
                self._upload(card.first_frame)
            self._fill(prompt, prompt_sha)
            self._final_check(expect_refs=1 if card.first_frame else 0)
        except DriverError as e:
            j.write("ABORTED", code=e.code, reason=str(e))
            raise

        if not send:
            j.write("DRY_RUN_OK", note="已上传并填写，未点击。请在页面上手动清空输入区。")
            return Result("DRY_RUN_OK", take_id, {"promptSHA256": prompt_sha})

        j.write("SEND_RESERVED", promptSHA256=prompt_sha, firstFrameSHA256=frame_sha,
                baselineAttachments=len(baseline))
        clicked_at = time.time()
        self._click_send_once()

        started = self._observe_start(base_agent)
        j.write("SENT" if started else "SEND_UNCONFIRMED", clickedAt=clicked_at)

        return self._await_receipt(j, take_id, base_keys, base_vsrc, base_agent)

    def _upload(self, path: str):
        inp = self.page.locator(f"form:has({self.s.composer_overlay}) {self.s.file_input}")
        if inp.count() == 0:
            inp = self.page.locator(self.s.file_input)
        if inp.count() == 0:
            raise DriverError("NO_FILE_INPUT", "找不到上传控件")
        inp.first.set_input_files(path)
        deadline = time.monotonic() + self.t.upload_confirm_s
        while time.monotonic() < deadline:
            if self.probe()["removeCount"] >= 1:
                return
            time.sleep(0.3)
        raise DriverError("UPLOAD_UNCONFIRMED", "首帧上传未确认（未出现“移除附件”），不点击")

    def _fill(self, prompt: str, prompt_sha: str):
        ov = self.page.locator(self.s.composer_overlay).first
        ta = self.page.locator(f"form:has({self.s.composer_overlay}) {self.s.textarea}")
        if ta.count() == 0:
            ta = self.page.locator(self.s.textarea)
        ta = ta.first
        ta.click()
        self.page.keyboard.insert_text(prompt)
        if sha256_text(ta.input_value()) != prompt_sha:
            ta.fill(prompt)  # React 受控组件兜底
        if sha256_text(ta.input_value()) != prompt_sha:
            raise DriverError("PROMPT_MISMATCH", "输入框文本与提示词 SHA 不一致，不点击")
        _ = ov

    def _final_check(self, expect_refs: int):
        p = self.probe()
        if p["removeCount"] != expect_refs:
            raise DriverError("REF_COUNT", f"附件数 {p['removeCount']} ≠ {expect_refs}")
        if p["dialogs"]:
            raise DriverError("DIALOG", "页面有弹窗，不点击")
        if p["quota"]:
            raise DriverError("QUOTA", "页面显示额度用尽/需升级")
        if p["send"] != "enabled":
            raise DriverError("SEND_BUTTON", f"发送按钮状态 {p['send']}")

    def _click_send_once(self):
        """只点一次。之后任何情况都不再点、不按回车。"""
        sel = ("button:visible, [role=button]:visible")
        btns = self.page.locator(sel)
        rx = re.compile(self.s.send_label_re, re.I)
        for i in range(btns.count()):
            b = btns.nth(i)
            label = (b.get_attribute("aria-label") or "") + " " + (b.get_attribute("data-testid") or "")
            if rx.search(label):
                b.click(no_wait_after=True)
                return
        # 预检已确认按钮存在；走到这里说明页面在点击前变了。此时已写 SEND_RESERVED，按“结果不明”处理。

    def _observe_start(self, base_agent: int) -> bool:
        deadline = time.monotonic() + self.t.send_observe_s
        while time.monotonic() < deadline:
            p = self.probe()
            if p["stop"] or p["agentBubbles"] > base_agent or not (p["textValue"] or "").strip():
                return True
            time.sleep(0.5)
        return False

    def _await_receipt(self, j: Journal, take_id, base_keys, base_vsrc, base_agent) -> Result:
        deadline = time.monotonic() + self.t.receipt_s
        last_seen, stable = None, 0
        idle_since = None
        while time.monotonic() < deadline:
            time.sleep(self.t.poll_s)
            atts = self.attachments()
            new = [a for a in atts
                   if (a["tid"], a["vsrc"], a["img"]) not in base_keys
                   and a["vsrc"] not in base_vsrc and _is_video(a)]
            if new:
                cand = new[-1]
                key = cand["vsrc"]
                stable = stable + 1 if key == last_seen else 1
                last_seen = key
                if stable >= 2:
                    return self._bind(j, take_id, cand)
                continue
            p = self.probe()
            if p["stop"]:
                idle_since = None
                continue
            if p["agentBubbles"] > base_agent:
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= self.t.text_only_grace_s:
                    text = self.page.evaluate(_JS_AGENT_TEXT, self.s.to_dict())[:500]
                    j.write("TEXT_ONLY", agentText=text)
                    return Result("TEXT_ONLY", take_id, {"agentText": text})
        j.write("NO_RECEIPT", waitedSeconds=self.t.receipt_s)
        return Result("NO_RECEIPT", take_id)

    def _bind(self, j: Journal, take_id: str, att: dict) -> Result:
        res = self.page.evaluate(_JS_FETCH_B64, att["vsrc"])
        data = base64.b64decode(res["b64"])
        if len(data) < 1024:
            raise DriverError("MEDIA_TOO_SMALL", f"取回 {len(data)} 字节")
        os.makedirs(j.dir, exist_ok=True)
        ext = ".webm" if "webm" in (res.get("type") or "") else ".mp4"
        out = os.path.join(j.dir, f"{take_id}{ext}")
        with open(out, "wb") as f:
            f.write(data)
        sha = hashlib.sha256(data).hexdigest()
        info = probe_media(out)
        if info is not None and not info.get("has_video"):
            raise DriverError("NOT_VIDEO", "取回的文件不是视频")
        ev = j.write("RECEIPT_BOUND", mediaId=att["tid"], mediaSrc=_redact_url(att["vsrc"]),
                     file=os.path.basename(out), sha256=sha, bytes=len(data), media=info)
        return Result("RECEIPT_BOUND", take_id, ev)


def probe_media(path: str) -> dict | None:
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate:format=duration",
                          "-of", "json", path], capture_output=True, text=True)
    if out.returncode != 0:
        return {"has_video": False, "error": out.stderr[-300:]}
    d = json.loads(out.stdout or "{}")
    v = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
    return {"has_video": bool(v),
            "width": v[0].get("width") if v else None,
            "height": v[0].get("height") if v else None,
            "fps": v[0].get("r_frame_rate") if v else None,
            "duration": float(d.get("format", {}).get("duration", 0) or 0)}
