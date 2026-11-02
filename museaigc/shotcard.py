"""镜头卡：一镜一个 JSON，发送前逐项核对。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass


class ShotCardError(Exception):
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


ASPECT_TEXT = {
    "16:9": "16:9 widescreen landscape video",
    "9:16": "9:16 vertical portrait video",
}


@dataclass
class ShotCard:
    shot_id: str
    prompt: str
    first_frame: str | None
    first_frame_sha256: str | None
    aspect: str = "16:9"
    duration_request_s: int = 10
    send_allowed: bool = False
    source_path: str = ""

    @classmethod
    def load(cls, path: str) -> "ShotCard":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        base = os.path.dirname(os.path.abspath(path))
        ff = d.get("firstFrame")
        if ff and not os.path.isabs(ff):
            ff = os.path.join(base, ff)
        for key in ("shotId", "prompt"):
            if not d.get(key):
                raise ShotCardError(f"镜头卡缺少字段 {key}")
        return cls(
            shot_id=d["shotId"],
            prompt=d["prompt"],
            first_frame=ff,
            first_frame_sha256=(d.get("firstFrameSHA256") or None),
            aspect=d.get("aspect", "16:9"),
            duration_request_s=int(d.get("durationRequestSeconds", 10)),
            send_allowed=bool(d.get("sendAllowed", False)),
            source_path=os.path.abspath(path),
        )

    def final_prompt(self) -> str:
        """画幅与时长写进提示词末尾（Muse 网页端没有独立参数，做法见 NOTICE.md）。"""
        aspect = ASPECT_TEXT.get(self.aspect, self.aspect)
        return f"{self.prompt.strip()}\n\n{aspect}，时长 {self.duration_request_s} 秒。"

    def verify_first_frame(self) -> str | None:
        """返回实测 SHA；镜头卡写了 SHA 就必须一致。"""
        if not self.first_frame:
            return None
        if not os.path.isfile(self.first_frame):
            raise ShotCardError(f"首帧文件不存在：{self.first_frame}")
        size = os.path.getsize(self.first_frame)
        if size > 10 * 1024 * 1024:
            raise ShotCardError(f"首帧 {size} 字节，超过 10MB")
        actual = sha256_file(self.first_frame)
        if not self.first_frame_sha256:
            raise ShotCardError(f"镜头卡未写 firstFrameSHA256（实测 {actual}），拒绝发送")
        if actual.lower() != self.first_frame_sha256.lower():
            raise ShotCardError(
                f"首帧 SHA 不符：镜头卡 {self.first_frame_sha256}，实测 {actual}")
        return actual
