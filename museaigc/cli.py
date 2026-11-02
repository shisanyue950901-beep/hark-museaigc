"""命令行：museaigc doctor | dry-run | send | status"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .driver import DriverError, MuseDriver, Timeouts
from .journal import Journal
from .selectors import Selectors
from .shotcard import ShotCard, ShotCardError


def _page(pw, cdp: str, base_url: str):
    browser = pw.chromium.connect_over_cdp(cdp)
    if not browser.contexts:
        raise DriverError("NO_CONTEXT", "Chrome 没有可用窗口；请用 scripts/start-chrome.ps1 启动")
    ctx = browser.contexts[0]
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    for p in ctx.pages:
        if host in p.url:
            return browser, p
    return browser, ctx.new_page()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="museaigc")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222", help="Chrome 远程调试地址")
    ap.add_argument("--selectors", help="选择器覆盖 JSON")
    ap.add_argument("--runs", default="runs", help="日志与成片目录")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="只读体检：不输入、不上传、不点击")
    for name in ("dry-run", "send"):
        sp = sub.add_parser(name)
        sp.add_argument("shot", help="镜头卡 JSON")
        sp.add_argument("--take", help="Take 编号，默认 <shotId>-T<时间>")
        sp.add_argument("--receipt-minutes", type=float, default=15)
    st = sub.add_parser("status")
    st.add_argument("take")
    a = ap.parse_args(argv)

    if a.cmd == "status":
        for e in Journal(a.runs, a.take).events():
            print(json.dumps(e, ensure_ascii=False))
        return 0

    from playwright.sync_api import sync_playwright

    sel = Selectors.load(a.selectors)
    try:
        with sync_playwright() as pw:
            browser, page = _page(pw, a.cdp, sel.base_url)
            if a.cmd == "doctor":
                d = MuseDriver(page, sel, runs_dir=a.runs).doctor()
                print(json.dumps(d, ensure_ascii=False, indent=2))
                return 0 if d["ok"] else 2
            card = ShotCard.load(a.shot)
            take = a.take or f"{card.shot_id}-T{time.strftime('%Y%m%d%H%M%S')}"
            t = Timeouts(receipt_s=a.receipt_minutes * 60)
            r = MuseDriver(page, sel, t, runs_dir=a.runs).run(card, take, send=(a.cmd == "send"))
            print(json.dumps({"state": r.state, "take": r.take_id, **r.data},
                             ensure_ascii=False, indent=2))
            # 不关闭用户的 Chrome，只断开连接
            return 0 if r.state in ("DRY_RUN_OK", "RECEIPT_BOUND") else 3
    except (DriverError, ShotCardError) as e:
        print(f"停止：{e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
