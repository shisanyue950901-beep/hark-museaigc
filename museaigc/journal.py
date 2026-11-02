"""只追加的 Take 日志。规则：一个 Take 只发一次，结果不明绝不补发。"""
from __future__ import annotations

import json
import os
import time

# 终态之外的状态都会阻止同一 Take 再次发送；只有从未 SEND_RESERVED 的 Take 能发。
STATES = (
    "PREPARED",          # 预检通过
    "DRY_RUN_OK",        # 已上传、已填写、未点击
    "SEND_RESERVED",     # 即将点击；此后该 Take 永不再点
    "SENT",              # 观察到开始生成（停止按钮或新消息）
    "SEND_UNCONFIRMED",  # 点了但没观察到开始生成；仍继续等回执，绝不补点
    "RECEIPT_BOUND",     # 新视频附件 + 已取回字节 + SHA
    "TEXT_ONLY",         # 只有文字回复（拒答/失败说明）
    "NO_RECEIPT",        # 超时未见新视频
    "ABORTED",           # 点击前中止
)


class JournalError(Exception):
    pass


class Journal:
    def __init__(self, runs_dir: str, take_id: str):
        self.take_id = take_id
        self.dir = os.path.join(runs_dir, take_id)
        self.path = os.path.join(self.dir, "journal.jsonl")

    def events(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def states(self) -> list[str]:
        return [e["state"] for e in self.events()]

    def ever_reserved(self) -> bool:
        return "SEND_RESERVED" in self.states()

    def write(self, state: str, **data) -> dict:
        if state not in STATES:
            raise JournalError(f"未知状态 {state}")
        if state == "SEND_RESERVED" and self.ever_reserved():
            raise JournalError(f"Take {self.take_id} 已发送过，禁止再次发送；请新开 Take")
        os.makedirs(self.dir, exist_ok=True)
        ev = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "take": self.take_id, "state": state, **data}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return ev
