"""命令行：museaigc doctor | dry-run | send | status"""
from __future__ import annotations

import argparse
import json
import os
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
    gp = sub.add_parser("graph", help="多镜头画布")
    gs = gp.add_subparsers(dest="gcmd", required=True)
    for name in ("validate", "status", "serve"):
        x = gs.add_parser(name); x.add_argument("graph")
        if name == "serve":
            x.add_argument("--port", type=int, default=8790)
            x.add_argument("--allow-generate", action="store_true",
                           help="允许在画布里点「生成」调用 muse2api（消耗额度）")
    x = gs.add_parser("brief"); x.add_argument("graph"); x.add_argument("node")
    x = gs.add_parser("pick"); x.add_argument("graph"); x.add_argument("node"); x.add_argument("file")
    x = gs.add_parser("gen", help="经 muse2api 生成（首帧出候选；视频需 --send）")
    x.add_argument("graph"); x.add_argument("node")
    x.add_argument("--send", action="store_true"); x.add_argument("--new-attempt", action="store_true")
    x = gs.add_parser("run"); x.add_argument("graph"); x.add_argument("node")
    x.add_argument("--send", action="store_true", help="视频节点真发（需节点 sendAllowed=true）")
    x.add_argument("--receipt-minutes", type=float, default=15)
    a = ap.parse_args(argv)

    if a.cmd == "graph":
        return _graph_cmd(a)

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


def _graph_cmd(a) -> int:
    from .graph import Graph, GraphError
    try:
        g = Graph.load(a.graph)
        if a.gcmd == "serve":
            from .canvas_server import serve
            serve(a.graph, a.port, allow_generate=a.allow_generate); return 0
        if a.gcmd == "gen":
            print(json.dumps(g.generate(a.node, send=a.send, new_attempt=a.new_attempt),
                             ensure_ascii=False, indent=2)); return 0
        if a.gcmd == "validate":
            for w in g.validate():
                print("警告：" + w)
            print("图有效"); return 0
        if a.gcmd == "status":
            g.validate()
            print(json.dumps(g.status(), ensure_ascii=False, indent=2)); return 0
        if a.gcmd == "brief":
            print(json.dumps(g.brief(a.node), ensure_ascii=False, indent=2)); return 0
        if a.gcmd == "pick":
            print(json.dumps(g.pick(a.node, a.file), ensure_ascii=False, indent=2)); return 0
        runner = None
        if g.nodes().get(a.node, {}).get("type") == "video":
            runner = _browser_video_runner(a)
        r = g.run(a.node, video_runner=runner, send=a.send)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("sha256") or r.get("state") == "DRY_RUN_OK" else 3
    except Exception as e:
        from .muse2api import Muse2APIError
        if not isinstance(e, (GraphError, DriverError, ShotCardError, Muse2APIError)):
            raise
        print(f"停止：{e}", file=sys.stderr)
        return 4


def _browser_video_runner(a):
    """把视频节点翻译成一张临时镜头卡，交给现有驱动（一个 Take 只发一次）。"""
    def run(g, nid, frame_path, frame_sha, prm, send):
        from playwright.sync_api import sync_playwright
        card = ShotCard(shot_id=nid, prompt=prm.get("prompt", ""), first_frame=frame_path,
                        first_frame_sha256=frame_sha, aspect=prm.get("aspect", "16:9"),
                        duration_request_s=int(prm.get("durationRequestSeconds", 10)),
                        send_allowed=bool(prm.get("sendAllowed")))
        if not card.prompt:
            raise ShotCardError(f"{nid}: 缺少 prompt")
        take = prm.get("take") or f"{nid}-T{time.strftime('%Y%m%d%H%M%S')}"
        sel = Selectors.load(a.selectors)
        runs = os.path.join(g.work, "runs")
        with sync_playwright() as pw:
            _, page = _page(pw, a.cdp, sel.base_url)
            r = MuseDriver(page, sel, Timeouts(receipt_s=a.receipt_minutes * 60),
                           runs_dir=runs).run(card, take, send=send)
        out = {"state": r.state, "take": take, **r.data}
        if r.state == "RECEIPT_BOUND":
            out["path"] = os.path.join(runs, take, r.data["file"])
        return out
    return run


if __name__ == "__main__":
    sys.exit(main())
