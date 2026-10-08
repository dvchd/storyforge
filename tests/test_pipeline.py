"""Kiểm thử đầu-cuối: app + worker giả lập, cả hai luồng, các cấp duyệt và các tình huống lỗi."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from helpers import FAST, HAS_FFMPEG, char, drain, make_env, needs_ffmpeg, new_project, statuses

from storyforge.app import assets, dedupe, engine, jobs, policy, state as st
from storyforge.app.db import db, jd, jl, now


def _assert_published(pid: int) -> None:
    """Có ffmpeg: phải có video. Không có: job dựng video phải lỗi với hướng dẫn cài tiếng Việt."""
    for ch in db.q("SELECT * FROM chapter WHERE project_id=?", pid):
        p = engine.project(pid)
        if engine.wants_comic(p):
            assert ch["comic_cbz_asset_id"] and ch["comic_pdf_asset_id"]
        if engine.wants_video(p):
            if HAS_FFMPEG:
                assert ch["video_asset_id"] and ch["srt_asset_id"]
            else:
                assert "Thiếu ffmpeg" in ch["error"] and "brew install ffmpeg" in ch["error"]


def test_full_auto_both_shares_master_images(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("both", "0")
    engine.advance_project(pid)
    drain(worker)
    assert statuses(pid) == ["done"] * 4
    beats = db.val("SELECT COUNT(*) FROM beat WHERE project_id=? AND status='approved'", pid)
    assert db.val("SELECT COUNT(*) FROM job WHERE project_id=? AND owner_type='beat_image'", pid) == beats
    assert db.val("SELECT COUNT(*) FROM segment WHERE project_id=?", pid) == beats
    assert db.val("SELECT COUNT(*) FROM panel WHERE project_id=?", pid) == beats
    # điểm lấy nét mặc định theo góc máy
    for r in db.q("SELECT s.focus_y fy, b.shot FROM segment s JOIN beat b ON b.id=s.beat_id WHERE s.project_id=?", pid):
        assert r["fy"] == engine.default_focus(r["shot"])[1]
    lam = char(pid, "Lâm An")
    s2, s3 = st.state_at(lam["id"], 2), st.state_at(lam["id"], 3)
    assert "mark" not in s2 and "scar" in s3["mark"] and st.face_key(s2) != st.face_key(s3)
    # trang phục tiếng Việt từ LLM giả lập bị gắn cờ
    ev = db.one("SELECT * FROM state_event WHERE subject_id=? AND field='outfit'", lam["id"])
    assert "non_english_prompt" in jl(ev["flags_json"], [])
    keys = {r["state_key"] for r in db.q("SELECT state_key FROM ref WHERE owner_type='location' AND project_id=?", pid)}
    assert any(k.startswith("night") for k in keys), keys
    tkc = db.one("SELECT * FROM location WHERE project_id=? AND name='Tàng Kinh Các'", pid)
    assert "ruined" in st.loc_state_at(tkc["id"], 4).get("condition", "")
    _assert_published(pid)
    c2 = db.one("SELECT id FROM chapter WHERE project_id=? AND idx=2", pid)["id"]
    tvt = db.one("SELECT id FROM location WHERE project_id=? AND name='Thanh Vân Tông'", pid)
    for url in ["/", f"/p/{pid}", f"/p/{pid}/chapters/table", f"/p/{pid}/c/{c2}", f"/p/{pid}/c/{c2}?tab=prod",
                f"/p/{pid}/c/{c2}?tab=state", f"/p/{pid}/review", f"/p/{pid}/review?entity=event",
                f"/p/{pid}/characters", f"/p/{pid}/characters/{lam['id']}", f"/p/{pid}/locations",
                f"/p/{pid}/locations/{tvt['id']}", f"/p/{pid}/refs", f"/p/{pid}/jobs", f"/p/{pid}/jobs/table",
                f"/p/{pid}/settings", f"/p/{pid}/audit", f"/p/{pid}/c/{c2}/preview/video",
                f"/p/{pid}/c/{c2}/preview/comic", f"/p/{pid}/bible.md", "/workers", "/storage", "/job/1"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:800])


def test_remove_single_mark_from_ui(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", story="modern_story")
    engine.advance_project(pid)
    drain(worker)
    quan = char(pid, "Quân")
    ch3 = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=3", pid)
    ch2 = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=2", pid)
    client.post(f"/character/{quan['id']}/mark/add", data={"mark": "scar on chin", "chapter_id": ch2["id"]})
    assert st.marks_of(st.state_at(quan["id"], 3)) == ["tattoo", "scar on chin"]
    page = client.get(f"/p/{pid}/characters/{quan['id']}").text
    assert "scar on chin" in page and "mark/remove" in page
    r = client.post(f"/character/{quan['id']}/mark/remove", data={"mark": "tattoo", "chapter_id": ch3["id"]},
                    headers={"hx-request": "true"})
    assert r.status_code == 200 and "đã xóa" in r.text
    assert st.marks_of(st.state_at(quan["id"], 2)) == ["tattoo", "scar on chin"], "chương trước không đổi"
    assert st.marks_of(st.state_at(quan["id"], 3)) == ["scar on chin"], "chỉ xóa hình xăm"
    drain(worker)
    face_keys = {r["state_key"] for r in db.q("SELECT state_key FROM ref WHERE owner_type='character_face' AND owner_id=?", quan["id"])}
    assert st.face_key(st.state_at(quan["id"], 3)) in face_keys, "ảnh mặt mới được tạo cho chương 3"


def test_stale_after_state_edit_and_lock(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "0", chapters=2)
    engine.advance_project(pid)
    drain(worker)
    lam = char(pid, "Lâm An")
    ev = db.one("SELECT * FROM state_event WHERE subject_id=? AND field='outfit'", lam["id"])
    client.post(f"/event/{ev['id']}/edit", data={"field": "outfit", "value": "black armor", "evidence": ev["evidence"]})
    drain(worker)
    p = engine.project(pid)
    ch2 = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=2", pid)
    beats = db.q("SELECT * FROM beat WHERE chapter_id=? AND cast_json LIKE ?", ch2["id"], f'%"character_id": {lam["id"]}%')
    info = engine.stale_info(p, ch2, beats[0])
    assert info["stale"] and info["refs_changed"]
    db.update("beat", beats[-1]["id"], image_locked=1)
    assert engine.regen_stale(ch2["id"]) == len(beats) - 1
    drain(worker)
    assert db.get("chapter", ch2["id"])["status"] == "done"
    assert not engine.stale_info(p, ch2, db.get("beat", beats[0]["id"]))["stale"]


def test_level2_manual_review_through_ui(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "2", chapters=2)
    engine.advance_project(pid)
    drain(worker)
    assert db.q("SELECT * FROM character WHERE project_id=? AND status='pending'", pid)
    assert statuses(pid)[0] == "review_state"
    for _ in range(30):
        for ent in ("character", "location", "event", "beat", "ref", "beat_image", "segment_audio"):
            client.post(f"/p/{pid}/review/bulk", data={"entity": ent, "action": "approve"})
        drain(worker)
        if all(x == "done" for x in statuses(pid)):
            break
    assert statuses(pid) == ["done", "done"]
    ch = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=1", pid)
    assert not ch["video_asset_id"], "cấp 2: xuất bản chờ người bấm"
    client.post(f"/c/{ch['id']}/publish")
    drain(worker)
    ch = db.get("chapter", ch["id"])
    assert ch["video_asset_id"] if HAS_FFMPEG else "Thiếu ffmpeg" in ch["error"]
    assert any(a["reviewer"].startswith("user") for a in db.q("SELECT reviewer FROM audit WHERE project_id=?", pid))


def test_missing_ffmpeg_gives_clear_error(tmp_path):
    s, client, worker = make_env(tmp_path, ffmpeg=str(tmp_path / "khong-co" / "ffmpeg"))
    pid = new_project("video", "0", chapters=1)
    engine.advance_project(pid)
    drain(worker)
    assert statuses(pid) == ["done"], "thiếu ffmpeg không chặn sản xuất, chỉ chặn bước dựng video"
    j = db.one("SELECT * FROM job WHERE project_id=? AND kind='local.render_video'", pid)
    assert j["status"] == "failed" and "Thiếu ffmpeg" in j["error"] and "winget" in j["error"]
    ch = db.one("SELECT * FROM chapter WHERE project_id=?", pid)
    assert "Xuất bản lỗi" in ch["error"]
    page = client.get(f"/p/{pid}").text
    assert "Thiếu" in page and "ffmpeg" in page


def test_reject_regenerates_and_prompt_override(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", chapters=1)
    engine.advance_project(pid)
    drain(worker)
    b = db.one("SELECT * FROM beat WHERE project_id=? LIMIT 1", pid)
    r = client.post(f"/review/beat_image/{b['id']}/reject", headers={"hx-request": "true"})
    assert r.status_code == 200 and "scene" in r.text
    drain(worker)
    b2 = db.get("beat", b["id"])
    assert b2["image_regen"] == 1 and b2["image_asset_id"] != b["image_asset_id"] and b2["image_status"] == "approved"
    client.post(f"/beat/{b['id']}/prompt", data={"prompt_override": "a lone red umbrella in snow", "regen": "1"})
    drain(worker)
    assert db.get("beat", b["id"])["image_prompt"] == "a lone red umbrella in snow"


def test_crop_mismatch_warning_and_sync(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("both", "0", chapters=1)
    engine.advance_project(pid)
    drain(worker)
    p = engine.project(pid)
    b = db.one("SELECT * FROM beat WHERE project_id=? ORDER BY idx LIMIT 1", pid)
    seg, pn = db.one("SELECT * FROM segment WHERE beat_id=?", b["id"]), db.one("SELECT * FROM panel WHERE beat_id=?", b["id"])
    assert not engine.crop_info(p, b, seg, pn)["mismatch"], "mặc định hai khung trùng nhau"
    # ảnh gốc rất rộng, video lấy nét sát trái, khung truyện sát phải -> hai vùng gần như không chung chủ thể
    db.update("beat", b["id"], img_width=3000, img_height=1000)
    db.update("segment", seg["id"], focus_x=0.0)
    db.update("panel", pn["id"], focus_x=1.0)
    b, seg, pn = db.get("beat", b["id"]), db.get("segment", seg["id"]), db.get("panel", pn["id"])
    info = engine.crop_info(p, b, seg, pn)
    assert info["mismatch"], info
    assert engine.crop_mismatches(p) >= 1
    html = client.get(f"/p/{pid}/c/{b['chapter_id']}?tab=prod").text
    assert "khung video và truyện lệch nhau" in html
    r = client.post(f"/beat/{b['id']}/sync-focus", data={"to": "panel"}, headers={"hx-request": "true"})
    assert r.status_code == 200
    assert not engine.crop_info(p, b, db.get("segment", seg["id"]), db.get("panel", pn["id"]))["mismatch"]
    r = client.post(f"/segment/{seg['id']}/frame", data={"focus": "face", "motion": "zoom_in"}, headers={"hx-request": "true"})
    assert r.status_code == 200 and db.get("segment", seg["id"])["focus_y"] == 0.35


def test_checkpoint_pauses(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", chapters=3)
    db.update("project", pid, policy_json=jd({"level": "0", "checkpoint_every": 2, "gates": {}}))
    engine.advance_project(pid)
    drain(worker)
    p = db.get("project", pid)
    assert p["paused"] == 1 and "2" in p["pause_reason"] and statuses(pid)[2] == "new"
    client.post(f"/p/{pid}/resume")
    drain(worker)
    assert statuses(pid) == ["done"] * 3


def test_bad_llm_json_retry_does_not_grow(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", chapters=1)
    from storyforge.worker.adapters import mock

    orig, calls = mock.MockLLM._extract, {"n": 0}

    def flaky(self, meta):
        calls["n"] += 1
        return {"oops": True, "events": "not-a-list"} if calls["n"] <= 2 else orig(self, meta)

    mock.MockLLM._extract = flaky
    try:
        engine.advance_project(pid)
        drain(worker)
    finally:
        mock.MockLLM._extract = orig
    assert statuses(pid) == ["done"]
    j = db.one("SELECT * FROM job WHERE project_id=? AND owner_type='chapter_extract'", pid)
    payload = jl(j["payload_json"], {})
    assert j["attempts"] == 3 and len(payload["messages"]) == 4
    assert payload["temperature"] > 0.2 and "không hợp lệ" in payload["messages"][-1]["content"]


def _patch_beats(mode: str):
    from storyforge.worker.adapters import mock

    orig, calls = mock.MockLLM._beats, {"n": 0}

    def gappy(self, meta):
        calls["n"] += 1
        out = orig(self, meta)
        if mode == "always" or calls["n"] == 1:
            out["beats"] = out["beats"][:1] + out["beats"][2:]
        return out

    mock.MockLLM._beats = gappy
    return lambda: setattr(mock.MockLLM, "_beats", orig)


def test_span_errors_retry_once_then_redo_ok(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", chapters=1)
    restore = _patch_beats("once")
    try:
        engine.advance_project(pid)
        drain(worker)
    finally:
        restore()
    j = db.one("SELECT * FROM job WHERE project_id=? AND owner_type='chapter_beats'", pid)
    assert j["attempts"] == 2 and "bỏ sót" in jl(j["payload_json"], {})["messages"][-1]["content"]
    assert not db.q("SELECT id FROM beat WHERE project_id=? AND flags_json LIKE '%span_adjusted%'", pid)


def test_span_errors_stop_after_one_retry_and_flag(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", chapters=1)
    restore = _patch_beats("always")
    try:
        engine.advance_project(pid)
        drain(worker)
    finally:
        restore()
    j = db.one("SELECT * FROM job WHERE project_id=? AND owner_type='chapter_beats'", pid)
    assert j["attempts"] == 2, "chỉ bắt LLM chia lại 1 lần, không đốt hết số lần thử"
    assert db.q("SELECT id FROM beat WHERE project_id=? AND flags_json LIKE '%span_adjusted%'", pid)
    assert statuses(pid) == ["done"]


def test_dedupe_modes_and_merge(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", chapters=2)
    engine.advance_project(pid)
    drain(worker)
    p = engine.project(pid)
    lam = char(pid, "Lâm An")
    ch2 = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=2", pid)
    an = engine.create_character(p, ch2, "An", [], "", "")
    assert "possible_duplicate" not in jl(an["flags_json"], []), "tên một chữ không tự gợi ý gộp"
    lan = engine.create_character(p, ch2, "L. An", [], "", "")
    assert "possible_duplicate" in jl(lan["flags_json"], [])
    assert any(x["a_id"] == lam["id"] and x["b_id"] == lan["id"] for x in dedupe.open_suggestions(pid))
    db.insert("state_event", project_id=pid, subject_type="character", subject_id=lan["id"], chapter_id=ch2["id"],
              chapter_idx=2, field="hair", value="short", status="approved", created_at=now())
    client.post("/merge/merge_character", data={"keep": lam["id"], "merge": lan["id"]})
    assert db.get("character", lan["id"]) is None
    assert "L. An" in jl(db.get("character", lam["id"])["aliases_json"], [])
    assert st.state_at(lam["id"], 2).get("hair") == "short"
    # chế độ chỉ LLM: không gợi ý theo tên, quét bị bỏ qua, LLM rà vẫn chạy
    db.update("project", pid, settings_json=jd({**FAST, "dedupe_mode": "llm_only"}))
    p = engine.project(pid)
    x = engine.create_character(p, ch2, "Lam An Nhien", [], "", "")
    assert "possible_duplicate" not in jl(x["flags_json"], []) and dedupe.scan(pid) == 0
    client.post(f"/p/{pid}/dedupe/llm")
    drain(worker)
    assert db.one("SELECT status FROM job WHERE owner_type='project_dedupe'")["status"] == "done"
    db.update("project", pid, settings_json=jd({**FAST, "dedupe_mode": "off"}))
    assert dedupe.enqueue_llm(pid) == 0


def test_cancel_running_command_job(tmp_path):
    s, client, worker = make_env(tmp_path)
    from storyforge.worker.adapters import build

    slow = build({"kind": "image.generate", "type": "command", "model": "slow",
                  "cmd": [sys.executable, "-c", "import time\nfor i in range(100):\n print(f'{i}%', flush=True); time.sleep(0.1)"]})
    worker.adapters = [a for a in worker.adapters if a.kind != "image.generate"] + [slow]
    pid = new_project("video", "0", chapters=1)
    jid = jobs.enqueue(pid, "image.generate", {"prompt": "x", "width": 64, "height": 64}, "test_cancel", 1)
    t = threading.Thread(target=worker.run_once)
    t.start()
    for _ in range(60):
        if (db.get("job", jid)["progress"] or 0) > 0:
            break
        time.sleep(0.2)
    assert (db.get("job", jid)["progress"] or 0) > 0
    jobs.cancel(jid)
    t.join(timeout=15)
    assert not t.is_alive() and db.get("job", jid)["status"] == "cancelled"


def test_progress_monotonic_and_watchdog(tmp_path):
    s, client, worker = make_env(tmp_path, stall={"image.generate": 60})
    jid = jobs.enqueue(None, "image.generate", {}, "x", 1)
    jobs.claim("w1", ["image.generate"], [])
    jobs.heartbeat(jid, "w1", 0.5, "nửa")
    t1 = db.get("job", jid)["last_progress_at"]
    jobs.heartbeat(jid, "w1", 0.2, "lùi")           # worker báo lùi: bỏ qua
    j = db.get("job", jid)
    assert j["progress"] == 0.5 and j["last_progress_at"] == t1
    db.update("job", jid, last_progress_at=now() - 120)
    jobs.heartbeat(jid, "w1", 0.5, "vẫn gửi heartbeat nhưng không tiến")
    assert jobs.watchdog() == 1
    j = db.get("job", jid)
    assert j["status"] == "queued" and j["stalls"] == 1 and "Đứng yên" in j["error"]
    assert jobs.heartbeat(jid, "w1")["cancel"], "worker cũ nhận lệnh dừng"
    # LLM không báo tiến độ: không bị watchdog thu hồi
    lid = jobs.enqueue(None, "llm.chat", {}, "y", 1)
    jobs.claim("w2", ["llm.chat"], [])
    db.update("job", lid, last_progress_at=now() - 99999, started_at=now() - 99999)
    jobs.watchdog()
    assert db.get("job", lid)["status"] == "running"
    rows = client.get("/p/1/jobs/table") if db.get("project", 1) else None
    assert rows is None or rows.status_code == 200


def test_claim_aging_prevents_starvation(tmp_path):
    s, client, worker = make_env(tmp_path, aging_seconds=60, aging_cap=5)
    old = jobs.enqueue(None, "tts.synthesize", {}, "a", 1, priority=0)
    new = jobs.enqueue(None, "tts.synthesize", {}, "a", 2, priority=2)
    db.update("job", old, created_at=now() - 60 * 4)
    assert jobs.claim("w", ["tts.synthesize"], [])["id"] == old
    assert jobs.queue_positions([{"id": new}]) == {new: 1}


def test_settings_page_validation_reset_recommended(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("both", "0", chapters=1)
    html = client.get(f"/p/{pid}/settings").text
    assert "Kiểm tra cài đặt" in html and "phóng to" in html          # FAST image_area quá nhỏ
    client.post(f"/p/{pid}/settings/recommended")
    p = engine.project(pid)
    assert not [w for w in engine.settings_warnings(p) if w["key"] == "image_area"]
    client.post(f"/p/{pid}/settings", data={"name": "X", "mode": "both", "video_width": "321", "video_height": "181",
                                            "comic_format": "page", "dedupe_mode": "fuzzy"})
    p = engine.project(pid)
    assert p["settings"]["video_width"] == 322 and p["settings"]["video_height"] == 182
    client.post(f"/p/{pid}/settings/reset?group=Video")
    p = engine.project(pid)
    assert p["settings"]["video_width"] == engine.DEFAULT_SETTINGS["video_width"]
    assert p["settings"]["comic_page_width"] == FAST["comic_page_width"], "nhóm khác giữ nguyên"


def test_gc_removes_orphans(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", chapters=1)
    engine.advance_project(pid)
    drain(worker)
    b = db.one("SELECT * FROM beat WHERE project_id=? LIMIT 1", pid)
    old_path = assets.path_of(b["image_asset_id"])
    engine.regen_beat_image(b["id"])
    drain(worker)
    db.ex("UPDATE asset SET created_at=?", now() - 7 * 86400)
    assert assets.gc(24, dry_run=True)["rows"] >= 1 and old_path.exists()
    assert assets.gc(24)["rows"] >= 1 and not old_path.exists()
    assert assets.path_of(db.get("beat", b["id"])["image_asset_id"]).exists()


def test_webtoon_modern_story(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", story="modern_story", comic_format="webtoon")
    engine.advance_project(pid)
    drain(worker)
    assert statuses(pid) == ["done"] * 3
    assert {"Hà", "Quân"} <= {c["name"] for c in engine.all_chars(pid)}
    pg = db.one("SELECT * FROM page WHERE project_id=? LIMIT 1", pid)
    assert pg["width"] == FAST["webtoon_width"] and pg["height"] > pg["width"]
    quan = db.one("SELECT * FROM location WHERE project_id=? AND name='Quán Gió'", pid)
    assert "lantern" in st.loc_state_at(quan["id"], 3).get("decor", "")


def test_migrate_v1_database(tmp_path):
    from fixtures_v1 import make_v1_db

    data = tmp_path / "data"
    make_v1_db(data / "storyforge.db")
    s, client, worker = make_env(tmp_path)
    assert list((data / "backups").glob("*before-v1-to-v3*.db"))
    assert db.val("SELECT value FROM meta WHERE key='schema_version'") == "3"
    assert [l["name"] for l in db.q("SELECT name FROM location")] == ["Thanh Vân Tông"]
    ev = db.one("SELECT * FROM state_event WHERE id=1")
    assert ev["subject_type"] == "character" and ev["field"] == "mark"
    assert [c["status"] for c in db.q("SELECT status FROM chapter ORDER BY idx")] == ["state_ready", "state_ready", "new"]
    assert {"heartbeat_at", "last_progress_at", "stalls"} <= set(db.columns("job"))
    assert client.get("/p/1").status_code == 200
    db.update("project", 1, autorun=1, settings_json=jd(FAST), policy_json=jd({**policy.DEFAULT_POLICY, "level": "0"}))
    engine.advance_project(1)
    drain(worker)
    assert statuses(1)[0] in ("done", "producing")


def test_migrate_v2_database(tmp_path):
    from fixtures_v1 import make_v2_db

    make_v2_db(tmp_path / "data" / "storyforge.db")
    s, client, worker = make_env(tmp_path)
    assert db.val("SELECT value FROM meta WHERE key='schema_version'") == "3"
    assert db.one("SELECT stalls FROM job LIMIT 1")["stalls"] == 0
    assert client.get("/p/1/jobs").status_code == 200


def test_worker_rejects_corrupt_asset(tmp_path):
    from storyforge.worker.adapters import RetryableError
    from storyforge.worker.runner import AssetCache

    class FakeClient:
        def fetch_asset(self, asset_id, dest: Path) -> str:
            dest.write_bytes(b"\x89PNG broken")
            return "deadbeef"

    try:
        AssetCache(FakeClient(), tmp_path / "c", max_mb=1).get(1, "abc123")
        raise AssertionError("phải báo lỗi")
    except RetryableError:
        pass


def test_command_adapter_placeholders(tmp_path):
    from PIL import Image

    from storyforge.protocol import ClaimedJob
    from storyforge.worker.adapters import JobContext, build

    ref = tmp_path / "ref.png"
    Image.new("RGB", (8, 8)).save(ref)
    script = ("import sys; from PIL import Image; a=sys.argv; d={'k': 1}; "
              "assert a[a.index('--image-paths')+1].endswith('ref.png'); "
              "Image.new('RGB',(int(a[a.index('-W')+1]),32)).save(a[a.index('-o')+1])")
    ad = build({"kind": "image.generate", "type": "command", "model": "fake",
                "cmd": [sys.executable, "-c", script, "-W", "{width}", "-o", "{output}"],
                "cmd_with_refs": [sys.executable, "-c", script, "{refs}", "-W", "{width}", "-o", "{output}"],
                "ref_flag": "--image-paths"})
    job = ClaimedJob(id=7, kind="image.generate", payload={"prompt": "x", "width": 48, "height": 32, "seed": 1,
                                                          "refs": [{"asset_id": 1, "role": "face"}]})
    res = ad.run(job, JobContext(workdir=tmp_path, fetch_asset=lambda a, s: ref))
    assert Image.open(res.files[0]).size == (48, 32)


@needs_ffmpeg
def test_video_render_real(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "0", chapters=1)
    engine.advance_project(pid)
    drain(worker)
    ch = db.one("SELECT * FROM chapter WHERE project_id=?", pid)
    assert ch["video_asset_id"] and "-->" in assets.path_of(ch["srt_asset_id"]).read_text("utf-8")
