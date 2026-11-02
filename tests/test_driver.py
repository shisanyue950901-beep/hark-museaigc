import json

import pytest

from museaigc.driver import DriverError, MuseDriver, Timeouts
from museaigc.journal import Journal
from museaigc.selectors import Selectors
from museaigc.shotcard import ShotCard, ShotCardError

FAST = Timeouts(login_s=5, upload_confirm_s=2, send_observe_s=3, receipt_s=8,
                poll_s=0.3, text_only_grace_s=1)


def drv(page, server, tmp_path, scenario="ok"):
    page.goto(f"{server}index.html?scenario={scenario}&delay=300")
    return MuseDriver(page, Selectors(base_url=server), FAST,
                      runs_dir=str(tmp_path / "runs"), log=lambda *a: None)


def clicks(page):
    return page.evaluate("window.__clicks")


def states(tmp_path, take):
    return Journal(str(tmp_path / "runs"), take).states()


def test_doctor_is_read_only(page, server, tmp_path):
    d = drv(page, server, tmp_path).doctor()
    assert d["ok"] and d["send"] == "disabled" and clicks(page) == 0


def test_happy_path_binds_receipt(page, server, tmp_path, card):
    r = drv(page, server, tmp_path).run(ShotCard.load(str(card)), "SH01-T1", send=True)
    assert r.state == "RECEIPT_BOUND"
    assert clicks(page) == 1
    assert len(r.data["sha256"]) == 64 and r.data["bytes"] > 1024
    assert r.data["mediaId"].startswith("hatch-chat-attachment-presentation-video")
    assert not r.data["mediaSrc"].count("?")
    assert states(tmp_path, "SH01-T1") == ["PREPARED", "SEND_RESERVED", "SENT", "RECEIPT_BOUND"]
    if r.data.get("media"):
        assert r.data["media"]["has_video"]


def test_dry_run_never_clicks(page, server, tmp_path, card):
    r = drv(page, server, tmp_path).run(ShotCard.load(str(card)), "SH01-D1", send=False)
    assert r.state == "DRY_RUN_OK" and clicks(page) == 0
    assert page.evaluate("document.getElementById('ta').value").startswith("夜间教室")


def test_same_take_never_sends_twice(page, server, tmp_path, card):
    d = drv(page, server, tmp_path)
    d.run(ShotCard.load(str(card)), "SH01-T2", send=True)
    page.goto(f"{server}index.html")
    with pytest.raises(DriverError) as e:
        d.run(ShotCard.load(str(card)), "SH01-T2", send=True)
    assert e.value.code == "ALREADY_SENT" and clicks(page) == 0


def test_upload_unconfirmed_no_click(page, server, tmp_path, card):
    with pytest.raises(DriverError) as e:
        drv(page, server, tmp_path, "upload_fail").run(ShotCard.load(str(card)), "T3", send=True)
    assert e.value.code == "UPLOAD_UNCONFIRMED" and clicks(page) == 0
    assert "SEND_RESERVED" not in states(tmp_path, "T3")


@pytest.mark.parametrize("scenario,code", [("quota", "QUOTA"), ("dialog", "DIALOG")])
def test_blockers_no_click(page, server, tmp_path, card, scenario, code):
    with pytest.raises(DriverError) as e:
        drv(page, server, tmp_path, scenario).run(ShotCard.load(str(card)), "T4", send=True)
    assert e.value.code == code and clicks(page) == 0


def test_cover_only_is_not_receipt(page, server, tmp_path, card):
    r = drv(page, server, tmp_path, "cover_only").run(ShotCard.load(str(card)), "T5", send=True)
    assert r.state == "NO_RECEIPT" and clicks(page) == 1


def test_refusal_is_text_only(page, server, tmp_path, card):
    r = drv(page, server, tmp_path, "refuse").run(ShotCard.load(str(card)), "T6", send=True)
    assert r.state == "TEXT_ONLY" and "没生成成功" in r.data["agentText"] and clicks(page) == 1


def test_old_video_in_baseline_ignored(page, server, tmp_path, card):
    r = drv(page, server, tmp_path, "old_video").run(ShotCard.load(str(card)), "T7", send=True)
    assert r.state == "RECEIPT_BOUND" and "old=1" not in r.data["mediaSrc"]


def test_no_start_is_unconfirmed_and_no_second_click(page, server, tmp_path, card):
    r = drv(page, server, tmp_path, "no_start").run(ShotCard.load(str(card)), "T8", send=True)
    assert r.state == "NO_RECEIPT" and clicks(page) == 1
    assert "SEND_UNCONFIRMED" in states(tmp_path, "T8")


def test_send_requires_send_allowed(page, server, tmp_path, card):
    d = json.loads(card.read_text(encoding="utf-8")); d["sendAllowed"] = False
    card.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(DriverError) as e:
        drv(page, server, tmp_path).run(ShotCard.load(str(card)), "T9", send=True)
    assert e.value.code == "SEND_NOT_ALLOWED"


def test_frame_sha_mismatch_refused(tmp_path, card):
    d = json.loads(card.read_text(encoding="utf-8")); d["firstFrameSHA256"] = "0" * 64
    card.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ShotCardError):
        ShotCard.load(str(card)).verify_first_frame()


def test_prompt_carries_aspect_and_duration(card):
    p = ShotCard.load(str(card)).final_prompt()
    assert "16:9 widescreen landscape video" in p and "5 秒" in p
