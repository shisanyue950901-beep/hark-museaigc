"""页面选择器（可用 JSON 覆盖）。

默认值参考 github.com/czg86389-hub/muse2api（MIT）engine.py 中公开的选择器，
本项目未在真实 muse.ai 上验证：状态 NOT_VERIFIED，以 `museaigc doctor` 实测为准。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field


@dataclass
class Selectors:
    base_url: str = "https://muse.ai/"
    composer_overlay: str = '[data-testid="hatch-composer-placeholder-overlay"]'
    textarea: str = "textarea"
    file_input: str = 'input[type="file"]'
    send_label_re: str = r"发送|send"
    stop_button: str = '[data-testid="hatch-composer-stop-button"]'
    remove_attachment_re: str = r"remove\s*attachment|移除附件|删除附件"
    attachment: str = '[data-testid^="hatch-chat-attachment-presentation-"]'
    agent_bubble: str = 'div[class*="hatch-chat-groupable-bubble"][class*="hatch-agent-bubble-bg"]'
    dialog: str = '[role="dialog"],[role="alertdialog"]'
    quota_re: str = r"升级到\s*Max|Upgrade to Max|额度已用完|out of (free )?(credits|generations)"
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None) -> "Selectors":
        if not path:
            return cls()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)
