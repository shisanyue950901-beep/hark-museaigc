import functools
import http.server
import json
import os
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK = os.path.join(ROOT, "mock")


@pytest.fixture(scope="session")
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=MOCK)
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


@pytest.fixture
def card(tmp_path):
    from museaigc.shotcard import sha256_file
    frame = tmp_path / "frame.png"
    frame.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"))
    d = {"shotId": "SH01", "prompt": "夜间教室，杨间低头看手机，随后抬眼。",
         "firstFrame": "frame.png", "firstFrameSHA256": sha256_file(str(frame)),
         "aspect": "16:9", "durationRequestSeconds": 5, "sendAllowed": True}
    p = tmp_path / "SH01.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p
