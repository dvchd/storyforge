"""Kiểm thử đầu-cuối: app + worker giả lập, cả hai luồng, các cấp duyệt và các tình huống lỗi."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from helpers import FAST, char, drain, make_env, new_project, statuses

from storyforge.app import assets, dedupe, engine, jobs, policy, state as st
from storyforge.app.db import db, jd, jl, now


def test_full_auto_both_shares_master_images(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("both", "0")
    engine.advance_project(pid)
    drain(worker)
    assert statuses(pid) == ["done"] * 4
    beats = db.val("SELECT COUNT(*) FROM beat WHERE project_id=? AND status='approved'", pid)
    scene_jobs = db.val("SELECT COUNT(*) FROM job WHERE project_id=? AND owner_type='beat_image'", pid)
    assert scene_jobs == beats, "mỗi nhịp chỉ tạo MỘT ảnh gốc cho cả video và truyện tranh"
    assert db.val("SELECT COUNT(*) FROM segment WHERE project_id=?", pid) == beats
    assert db.val("SELECT COUNT(*) FROM panel WHERE project_id=?", pid) == beats
    lam = char(pid, "Lâm An")
    # sẹo vĩnh viễn ở chương 3 -> ảnh tham chiếu mặt mới từ chương 3, chương 2 vẫn mặt cũ
    s2, s3 = st.state_at(lam["id"], 2), st.state_at(lam["id"], 3)
    assert "mark" not in s2 and "scar" in s3["mark"]
    assert st.face_key(s2) != st.face_key(s3)
    faces = db.q("SELECT state_key FROM ref WHERE owner_type='character_face' AND owner_id=?", lam["id"])
    assert {st.face_key(s2), st.face_key(s3)} <= {f["state_key"] for f in faces}
    # bối cảnh: biến thể đêm ở chương 4 và hiện trạng bị phá hủy sau khi cháy
    tvt = db.one("SELECT * FROM location WHERE project_id=? AND name='Thanh Vân Tông'", pid)
    keys = {r["state_key"] for r in db.q("SELECT state_key FROM ref WHERE owner_type='location' AND project_id=?", pid)}
    assert any(k.startswith("night") for k in keys), keys
    tkc = db.one("SELECT * FROM location WHERE project_id=? AND name='Tàng Kinh Các'", pid)
    assert tkc and "ruined" in st.loc_state_at(tkc["id"], 4).get("condition", "")
    for ch in db.q("SELECT * FROM chapter WHERE project_id=?", pid):
        assert ch["video_asset_id"] and ch["srt_asset_id"] and ch["comic_cbz_asset_id"] and ch["comic_pdf_asset_id"]
    srt = assets.path_of(db.one("SELECT srt_asset_id FROM chapter WHERE project_id=? AND idx=1", pid)["srt_asset_id"]).read_text("utf-8")
    assert "-->" in srt
    # mọi trang giao diện đều mở được
    c2 = db.one("SELECT id FROM chapter WHERE project_id=? AND idx=2", pid)["id"]
    for url in ["/", f"/p/{pid}", f"/p/{pid}/chapters/table", f"/p/{pid}/c/{c2}", f"/p/{pid}/c/{c2}?tab=prod",
                f"/p/{pid}/c/{c2}?tab=text", f"/p/{pid}/review", f"/p/{pid}/review?entity=event",
                f"/p/{pid}/characters", f"/p/{pid}/characters/{lam['id']}", f"/p/{pid}/locations",
                f"/p/{pid}/locations/{tvt['id']}", f"/p/{pid}/refs", f"/p/{pid}/refs?kind=location", f"/p/{pid}/jobs",
                f"/p/{pid}/jobs/table", f"/p/{pid}/settings", f"/p/{pid}/audit", f"/p/{pid}/c/{c2}/preview/video",
                f"/p/{pid}/c/{c2}/preview/comic", f"/p/{pid}/bible.md", "/workers", "/storage", "/job/1"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:800])
    assert "Lâm An" in client.get(f"/p/{pid}/bible.md").text


def test_stale_after_state_edit_and_lock(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "0", chapters=2)
    engine.advance_project(pid)
    drain(worker)
    lam = char(pid, "Lâm An")
    ev = db.one("SELECT * FROM state_event WHERE subject_id=? AND field='outfit'", lam["id"])
    client.post(f"/event/{ev['id']}/edit", data={"field": "outfit", "value": "black armor", "evidence": ev["evidence"]})
    drain(worker)   # tạo ảnh tham chiếu trang phục mới
    p = engine.project(pid)
    ch2 = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=2", pid)
    beats = db.q("SELECT * FROM beat WHERE chapter_id=? AND cast_json LIKE ?", ch2["id"], f'%"character_id": {lam["id"]}%')
    assert beats
    info = engine.stale_info(p, ch2, beats[0])
    assert info["stale"] and info["refs_changed"]
    db.update("beat", beats[-1]["id"], image_locked=1)
    n = engine.regen_stale(ch2["id"])
    assert n == len(beats) - 1, "ảnh đã khóa không bị tạo lại"
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
    assert db.get("chapter", ch["id"])["video_asset_id"]
    reviewers = {a["reviewer"] for a in db.q("SELECT reviewer FROM audit WHERE project_id=?", pid)}
    assert any(r.startswith("user") for r in reviewers)


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


def test_checkpoint_pauses(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "0", chapters=3)
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
    pid = new_project("video", "0", chapters=1)
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
    assert j["attempts"] == 3 and len(payload["messages"]) == 4, "chỉ giữ prompt gốc + lần trả lời sai gần nhất"
    assert payload["temperature"] > 0.2 and "không hợp lệ" in payload["messages"][-1]["content"]


def test_span_errors_ask_llm_to_redo(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "0", chapters=1)
    from storyforge.worker.adapters import mock

    orig, calls = mock.MockLLM._beats, {"n": 0}

    def gappy(self, meta):
        calls["n"] += 1
        out = orig(self, meta)
        if calls["n"] == 1:
            out["beats"] = out["beats"][:1] + out["beats"][2:]    # bỏ sót đoạn
        return out

    mock.MockLLM._beats = gappy
    try:
        engine.advance_project(pid)
        drain(worker)
    finally:
        mock.MockLLM._beats = orig
    j = db.one("SELECT * FROM job WHERE project_id=? AND owner_type='chapter_beats'", pid)
    assert j["attempts"] == 2 and "bỏ sót" in jl(j["payload_json"], {})["messages"][-1]["content"]
    assert not db.q("SELECT id FROM beat WHERE project_id=? AND flags_json LIKE '%span_adjusted%'", pid)


def test_dedupe_suggest_and_merge(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "0", chapters=2)
    engine.advance_project(pid)
    drain(worker)
    lam = char(pid, "Lâm An")
    ch2 = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=2", pid)
    an = engine.create_character(engine.project(pid), ch2, "An", [], "", "")
    assert "possible_duplicate" in jl(an["flags_json"], [])
    sug = dedupe.open_suggestions(pid)
    assert any(x["a_id"] == lam["id"] and x["b_id"] == an["id"] for x in sug)
    db.insert("state_event", project_id=pid, subject_type="character", subject_id=an["id"], chapter_id=ch2["id"],
              chapter_idx=2, field="hair", value="short", status="approved", created_at=now())
    r = client.post("/merge/merge_character", data={"keep": lam["id"], "merge": an["id"]})
    assert r.status_code in (200, 303)
    assert db.get("character", an["id"]) is None
    assert "An" in jl(db.get("character", lam["id"])["aliases_json"], [])
    assert st.state_at(lam["id"], 2).get("hair") == "short"
    # LLM rà trùng chạy qua hàng đợi
    client.post(f"/p/{pid}/dedupe/llm")
    drain(worker)
    assert db.one("SELECT status FROM job WHERE owner_type='project_dedupe'")["status"] == "done"


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
    for _ in range(50):
        if db.get("job", jid)["status"] == "running":
            break
        time.sleep(0.1)
    for _ in range(40):
        if (db.get("job", jid)["progress"] or 0) > 0:
            break
        time.sleep(0.2)
    assert (db.get("job", jid)["progress"] or 0) > 0, "tiến độ được báo về từ log của lệnh"
    jobs.cancel(jid)
    t.join(timeout=15)
    assert not t.is_alive() and db.get("job", jid)["status"] == "cancelled"


def test_claim_aging_prevents_starvation(tmp_path):
    s, client, worker = make_env(tmp_path, aging_seconds=60, aging_cap=5)
    old = jobs.enqueue(None, "tts.synthesize", {}, "a", 1, priority=0)
    new = jobs.enqueue(None, "tts.synthesize", {}, "a", 2, priority=2)
    db.update("job", old, created_at=now() - 60 * 4)     # chờ 4 phút: +4 điểm
    assert jobs.claim("w", ["tts.synthesize"], [])["id"] == old
    assert jobs.queue_positions([{"id": new}]) == {new: 1}


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
    dry = assets.gc(24, dry_run=True)
    assert dry["rows"] >= 1 and old_path.exists()
    r = assets.gc(24)
    assert r["rows"] >= 1 and not old_path.exists()
    assert assets.path_of(db.get("beat", b["id"])["image_asset_id"]).exists(), "ảnh đang dùng không bị xóa"
    assert client.get("/storage").status_code == 200


def test_webtoon_modern_story(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0", story="modern_story", comic_format="webtoon")
    engine.advance_project(pid)
    drain(worker)
    assert statuses(pid) == ["done"] * 3
    names = {c["name"] for c in engine.all_chars(pid)}
    assert {"Hà", "Quân"} <= names, names
    pg = db.one("SELECT * FROM page WHERE project_id=? LIMIT 1", pid)
    assert pg["width"] == FAST["webtoon_width"] and pg["height"] > pg["width"]
    assert all(p["w"] == 1.0 for p in db.q("SELECT w FROM panel WHERE project_id=?", pid))
    quan = db.one("SELECT * FROM location WHERE project_id=? AND name='Quán Gió'", pid)
    assert "lantern" in st.loc_state_at(quan["id"], 3).get("decor", "")


def test_migrate_v1_database(tmp_path):
    from fixtures_v1 import make_v1_db

    data = tmp_path / "data"
    make_v1_db(data / "storyforge.db")
    s, client, worker = make_env(tmp_path)
    assert list((data / "backups").glob("*before-v1-to-v2*.db")), "phải sao lưu trước khi nâng cấp"
    assert db.val("SELECT value FROM meta WHERE key='schema_version'") == "2"
    assert [l["name"] for l in db.q("SELECT name FROM location")] == ["Thanh Vân Tông"]
    ev = db.one("SELECT * FROM state_event WHERE id=1")
    assert ev["subject_type"] == "character" and ev["field"] == "mark"
    assert [c["status"] for c in db.q("SELECT status FROM chapter ORDER BY idx")] == ["state_ready", "state_ready", "new"]
    assert "notes" in db.columns("character") and "first_chapter" in db.columns("ref")
    assert not db.q("SELECT * FROM ref WHERE owner_type='location'")
    assert client.get("/p/1").status_code == 200
    db.update("project", 1, autorun=1, settings_json=jd(FAST), policy_json=jd({**policy.DEFAULT_POLICY, "level": "0"}))
    engine.advance_project(1)
    drain(worker)
    assert statuses(1)[0] in ("done", "producing")


def test_worker_rejects_corrupt_asset(tmp_path):
    from storyforge.worker.adapters import RetryableError
    from storyforge.worker.runner import AssetCache

    class FakeClient:
        def fetch_asset(self, asset_id, dest: Path) -> str:
            dest.write_bytes(b"\x89PNG broken")
            return "deadbeef"

    cache = AssetCache(FakeClient(), tmp_path / "c", max_mb=1)
    try:
        cache.get(1, "abc123")
        raise AssertionError("phải báo lỗi")
    except RetryableError:
        pass
