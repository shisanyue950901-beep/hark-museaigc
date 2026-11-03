"""浏览器端到端：设置 → 新建工程 → 上传参考图 → 生成首帧 → 选这张 → 生成视频（假产品 API）。"""
import json
import threading
import time
from http.server import ThreadingHTTPServer

from museaigc.app import App, make_app_handler
from test_muse2api import FakeOpen, _png


def test_app_end_to_end(tmp_path, monkeypatch):
    for k in ("MUSE2API_KEY", "MUSE2API_BASE", "MUSE2API_API", "MUSEAIGC_FFMPEG"):
        monkeypatch.setenv(k, "x"); monkeypatch.delenv(k)
    png = str(tmp_path / "ref.png"); _png(png, "orange")
    f = FakeOpen(png)
    app = App(str(tmp_path / "ws"))
    h = ThreadingHTTPServer(("127.0.0.1", 0), make_app_handler(app))
    threading.Thread(target=h.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{h.server_address[1]}"
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-proxy-server"]); pg = b.new_page()
            errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/")
            pg.fill("#base", f.base); pg.fill("#key", "k"); pg.click("#saveset"); pg.wait_for_timeout(400)
            pg.click("#test"); pg.wait_for_timeout(600)
            assert "连接成功" in pg.inner_text("#setmsg")
            pg.fill("#pname", "咒与鬼 片头"); pg.click("#create"); pg.wait_for_url("**/canvas"); pg.wait_for_timeout(600)
            pg.click('.node[data-id="REF1"] .hd'); pg.set_input_files("#upl", png); pg.wait_for_timeout(1200)
            pg.click('.node[data-id="KF1"] .hd'); pg.fill("#pt", "夜间教室"); pg.click("#gen"); pg.wait_for_timeout(2500)
            pg.click("[data-pick]"); pg.wait_for_timeout(800)
            pg.click('.node[data-id="V1"] .hd'); pg.fill("#pt", "镜头缓慢推近"); pg.click("#gen"); pg.wait_for_timeout(4000)
            st = json.loads(pg.evaluate("fetch('/api/status').then(r=>r.text())"))
            b.close()
        assert st["status"] == {"REF1": "DONE", "KF1": "DONE", "V1": "DONE"}
        assert errs == []
        assert [x[0] for x in f.posts] == ["/api/open/v1/images/img2img", "/api/open/v1/videos/img2video"]
    finally:
        f.httpd.shutdown(); h.shutdown()
