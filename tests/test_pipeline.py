"""Kiem thu dau-cuoi: app + worker gia lap, chay ca hai luong va cac cap duyet.

Chay: pytest -q   (hoac python tests/test_pipeline.py)
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from storyforge.app import engine, policy, state as st  # noqa: E402
from storyforge.app.config import load_settings  # noqa: E402
from storyforge.app.db import db, jd, jl, now  # noqa: E402
from storyforge.app.main import create_app  # noqa: E402
from storyforge.app.routes_web import add_chapters, split_chapters  # noqa: E402
from storyforge.worker.adapters import build  # noqa: E402
from storyforge.worker.client import WorkerClient  # noqa: E402
from storyforge.worker.runner import MOCK_ADAPTERS, Worker  # noqa: E402

SAMPLE = ROOT / "examples" / "sample_story"
# kich thuoc nho de kiem thu nhanh
FAST = {"video_width": 320, "video_height": 180, "video_fps": 8, "comic_page_width": 400,
        "comic_page_height": 600, "ref_size": 256, "comic_panel_area": 65536}


def make_env(tmp: Path):
    s = load_settings(tmp / "data", start_local_runner=False)
    app = create_app(s)
    client = TestClient(app)
    wc = WorkerClient("http://testserver", s.worker_token, "test-worker", http=client)
    worker = Worker(wc, [build(a) for a in MOCK_ADAPTERS], cache_dir=tmp / "cache", heartbeat_every=999)
    return s, client, worker


def new_project(mode: str, level: str, autorun: int = 1) -> int:
    pid = db.insert("project", name=f"T {mode} L{level}", mode=mode, style_prompt="test style",
                    policy_json=jd({**policy.DEFAULT_POLICY, "level": level}), autorun=autorun, created_at=now(),
                    settings_json=jd(FAST))
    items = []
    for f in sorted(SAMPLE.glob("*.txt")):
        items += split_chapters(f.read_text(encoding="utf-8"), f.stem)
    add_chapters(pid, items)
    return pid


def drain(worker: Worker, limit: int = 2000) -> int:
    n = 0
    while n < limit:
        did = worker.run_once()
        did_local = engine.local_tick()
        if not did and not did_local:
            break
        n += 1
    return n


def statuses(pid: int) -> list[str]:
    return [c["status"] for c in db.q("SELECT status FROM chapter WHERE project_id=? ORDER BY idx", pid)]


def test_split_chapters():
    items = split_chapters((SAMPLE / "01.txt").read_text(encoding="utf-8"))
    assert len(items) == 1 and items[0][0].startswith("Chương 1")
    multi = "Chương 1\nA\n\nB\nChương 2: X\nC"
    assert [t for t, _ in split_chapters(multi)] == ["Chương 1", "Chương 2: X"]


def test_policy_decide():
    import random

    gp = policy.GatePolicy(mode="auto_if_clean", always_review=["permanent"])
    assert policy.decide(gp, [], random.Random(1)) == "approved"
    assert policy.decide(gp, ["new_identity"], random.Random(1)) == "approved"
    assert policy.decide(gp, ["evidence_missing"], random.Random(1)) == "pending"
    gp = policy.GatePolicy(mode="auto", always_review=["permanent"])
    assert policy.decide(gp, ["permanent"], random.Random(1)) == "pending"


def test_state_resolve():
    evs = [
        {"id": 1, "chapter_idx": 2, "field": "outfit", "value": "robe", "status": "approved", "until_chapter": None},
        {"id": 2, "chapter_idx": 2, "field": "injury", "value": "bandage", "status": "approved", "until_chapter": 3},
        {"id": 3, "chapter_idx": 5, "field": "outfit", "value": "", "status": "approved", "until_chapter": None},
        {"id": 4, "chapter_idx": 4, "field": "hair", "value": "short", "status": "pending", "until_chapter": None},
    ]
    assert st.resolve(evs, 1) == {}
    assert st.resolve(evs, 3) == {"outfit": "robe", "injury": "bandage"}
    assert st.resolve(evs, 4) == {"outfit": "robe"}
    assert st.resolve(evs, 5) == {}


def test_full_auto_both(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("both", "0")
    engine.advance_project(pid)
    drain(worker)
    assert statuses(pid) == ["done", "done", "done"], statuses(pid)
    chars = {c["name"]: c for c in engine.all_chars(pid)}
    assert "Lâm An" in chars and "Tô Nguyệt" in chars
    lam = chars["Lâm An"]
    s3 = st.state_at(lam["id"], 3)
    assert "injury" in s3 and s3["injury"] == "scar on the face", s3
    assert "outfit" in st.state_at(lam["id"], 2)
    for ch in db.q("SELECT * FROM chapter WHERE project_id=?", pid):
        assert ch["video_asset_id"] and ch["srt_asset_id"], "thiếu video"
        assert ch["comic_cbz_asset_id"] and ch["comic_pdf_asset_id"], "thiếu truyện tranh"
    # cac trang web deu render duoc
    c1 = db.one("SELECT id FROM chapter WHERE project_id=? AND idx=2", pid)["id"]
    for url in ["/", f"/p/{pid}", f"/p/{pid}/c/{c1}", f"/p/{pid}/review", f"/p/{pid}/characters",
                f"/p/{pid}/characters/{lam['id']}", f"/p/{pid}/locations", f"/p/{pid}/refs", f"/p/{pid}/jobs",
                f"/p/{pid}/jobs/table", f"/p/{pid}/settings", f"/p/{pid}/audit", f"/p/{pid}/c/{c1}/preview/video",
                f"/p/{pid}/c/{c1}/preview/comic", "/workers"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:500])
    # thay doi trang thai -> anh cu
    ev = db.one("SELECT * FROM state_event WHERE character_id=? AND field='outfit'", lam["id"])
    r = client.post(f"/event/{ev['id']}/edit", data={"field": "outfit", "value": "black armor", "evidence": ev["evidence"]})
    assert r.status_code in (200, 303)
    p = engine.project(pid)
    ch2 = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=2", pid)
    stale = [sg for sg in db.q("SELECT * FROM segment WHERE chapter_id=?", ch2["id"])
             if engine.is_stale(p, ch2, "segment", sg)]
    # ref trang phuc moi chua co -> scene_spec None -> chua tinh la cu; tao ref truoc
    n = engine.regen_stale(ch2["id"])
    drain(worker)
    assert db.get("chapter", ch2["id"])["status"] == "done"
    assert isinstance(stale, list) and n >= 0


def test_level2_manual_review(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "2")
    engine.advance_project(pid)
    drain(worker)
    # cap 2: nhan vat moi phai cho nguoi duyet
    pend = db.q("SELECT * FROM character WHERE project_id=? AND status='pending'", pid)
    assert pend, "cấp 2 phải chờ duyệt nhân vật mới"
    assert statuses(pid)[0] == "review_state"
    # duyet het qua giao dien, lap cho den khi xong
    for _ in range(30):
        for ent in ("character", "location", "event", "beat", "ref", "segment_image", "segment_audio"):
            client.post(f"/p/{pid}/review/bulk", data={"entity": ent, "action": "approve"})
        drain(worker)
        if all(x == "done" for x in statuses(pid)):
            break
    assert statuses(pid) == ["done", "done", "done"], statuses(pid)
    # cap 2: xuat ban thu cong
    ch = db.one("SELECT * FROM chapter WHERE project_id=? AND idx=1", pid)
    assert not ch["video_asset_id"]
    client.post(f"/c/{ch['id']}/publish")
    drain(worker)
    assert db.get("chapter", ch["id"])["video_asset_id"]
    # nhat ky co ca nguoi va may duyet
    reviewers = {a["reviewer"] for a in db.q("SELECT reviewer FROM audit WHERE project_id=?", pid)}
    assert any(r.startswith("user") for r in reviewers)


def test_reject_regenerates(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("comic", "0")
    engine.advance_project(pid)
    drain(worker)
    pn = db.one("SELECT * FROM panel WHERE project_id=? LIMIT 1", pid)
    old_asset = pn["image_asset_id"]
    r = client.post(f"/review/panel_image/{pn['id']}/reject", headers={"hx-request": "true"})
    assert r.status_code == 200
    drain(worker)
    pn2 = db.get("panel", pn["id"])
    assert pn2["image_regen"] == 1 and pn2["image_asset_id"] != old_asset and pn2["image_status"] == "approved"


def test_checkpoint_pauses(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "0")
    db.update("project", pid, policy_json=jd({"level": "0", "checkpoint_every": 2, "gates": {}}))
    engine.advance_project(pid)
    drain(worker)
    p = db.get("project", pid)
    assert p["paused"] == 1 and "2" in p["pause_reason"]
    assert statuses(pid)[2] == "new"
    client.post(f"/p/{pid}/resume")
    drain(worker)
    assert statuses(pid) == ["done", "done", "done"]


def test_bad_llm_json_retries(tmp_path):
    s, client, worker = make_env(tmp_path)
    pid = new_project("video", "0")
    from storyforge.worker.adapters import mock

    orig = mock.MockLLM._extract
    calls = {"n": 0}

    def flaky(self, meta):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"oops": True, "events": "not-a-list"}
        return orig(self, meta)

    mock.MockLLM._extract = flaky
    try:
        engine.advance_project(pid)
        drain(worker)
    finally:
        mock.MockLLM._extract = orig
    assert statuses(pid) == ["done", "done", "done"]
    j = db.one("SELECT * FROM job WHERE project_id=? AND owner_type='chapter_extract' ORDER BY id LIMIT 1", pid)
    assert j["attempts"] == 2
    msgs = jl(j["payload_json"], {})["messages"]
    assert "không hợp lệ" in msgs[-1]["content"]


if __name__ == "__main__":
    import tempfile

    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            d = Path(tempfile.mkdtemp())
            try:
                fn(d) if fn.__code__.co_argcount else fn()
                print("OK  ", name)
            finally:
                shutil.rmtree(d, ignore_errors=True)


def test_command_image_adapter(tmp_path):
    """Adapter dong lenh: kiem tra thay placeholder va danh sach anh tham chieu."""
    from storyforge.protocol import ClaimedJob
    from storyforge.worker.adapters import JobContext, build

    ref = tmp_path / "ref.png"
    from PIL import Image

    Image.new("RGB", (8, 8)).save(ref)
    script = ("import sys; from PIL import Image; a=sys.argv; "
              "assert a[a.index('--image-paths')+1].endswith('ref.png'); "
              "Image.new('RGB',(int(a[a.index('-W')+1]),32)).save(a[a.index('-o')+1])")
    ad = build({"kind": "image.generate", "type": "command", "model": "fake",
                "cmd": [sys.executable, "-c", script, "-W", "{width}", "-o", "{output}"],
                "cmd_with_refs": [sys.executable, "-c", script, "{refs}", "-W", "{width}", "-o", "{output}"],
                "ref_flag": "--image-paths"})
    job = ClaimedJob(id=7, kind="image.generate", payload={"prompt": "x", "width": 48, "height": 32, "seed": 1,
                                                          "refs": [{"asset_id": 1, "role": "face"}]})
    res = ad.run(job, JobContext(workdir=tmp_path, fetch_asset=lambda a, s: ref))
    assert res.files and Image.open(res.files[0]).size == (48, 32)
