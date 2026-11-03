import json
import threading
from http.server import ThreadingHTTPServer

from museaigc.canvas_server import make_handler
from museaigc.graph import Graph

from test_graph import g  # noqa: F401  复用示例图夹具


def test_canvas_renders_and_wires(g, page):  # noqa: F811
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(g.path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{httpd.server_address[1]}/")
        page.wait_for_selector(".node")
        assert page.locator(".node").count() == 12
        assert page.locator("#wires path").count() == 14
        # 从 C01 的输出点拖到 SH02 尾部的 trim 不合法 → 保存被拒；改连到 SH01_v.frame 也超额 → 被拒
        src = page.locator('.node[data-id="C01"] [data-out]').bounding_box()
        dst = page.locator('.node[data-id="SH01_cut"] .in .dot').bounding_box()
        page.mouse.move(src["x"] + 5, src["y"] + 5); page.mouse.down()
        page.mouse.move(dst["x"] + 5, dst["y"] + 5); page.mouse.up()
        assert page.locator("#wires path").count() == 15
        page.click("#save")
        page.wait_for_function("document.querySelector('#msg').textContent.includes('未保存')")
        assert len(Graph.load(g.path).edges()) == 14
        # 删掉这条非法连线后保存成功
        page.locator("#wires path").nth(14).click(force=True)
        page.click("#save")
        page.wait_for_function("document.querySelector('#msg').textContent.startsWith('图有效') || document.querySelector('#msg').textContent.startsWith('已保存')")
        assert len(Graph.load(g.path).edges()) == 14
    finally:
        httpd.shutdown()
