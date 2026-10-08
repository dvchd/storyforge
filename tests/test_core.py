"""Kiểm thử đơn vị cho phần lõi không cần app chạy."""
from __future__ import annotations

import random

from helpers import EXAMPLES, make_env  # noqa: F401  (đảm bảo sys.path)

from storyforge.app import checks, comic, llm, policy, state as st, video
from storyforge.app.routes_web import split_chapters


def test_split_chapters():
    items = split_chapters((EXAMPLES / "sample_story" / "01.txt").read_text(encoding="utf-8"))
    assert len(items) == 1 and items[0][0].startswith("Chương 1")
    assert [t for t, _ in split_chapters("Chương 1\nA\n\nB\nChương 2: X\nC\nHồi 3\nD")] == ["Chương 1", "Chương 2: X", "Hồi 3"]


def test_policy_decide_and_info_flags():
    gp = policy.GatePolicy(mode="auto_if_clean", always_review=["permanent"])
    assert policy.decide(gp, [], random.Random(1)) == "approved"
    assert policy.decide(gp, ["new_identity"], random.Random(1)) == "approved"      # chỉ là thông tin
    assert policy.decide(gp, ["evidence_missing"], random.Random(1)) == "pending"
    assert policy.decide(policy.GatePolicy(mode="auto", always_review=["permanent"]), ["permanent"], random.Random(1)) == "pending"


def test_policy_chapter_level_keeps_project_overrides():
    project = {"policy_json": '{"level": "2", "gates": {"audio": {"mode": "manual"}}}'}
    chapter = {"policy_json": '{"level": "0"}'}
    eff = policy.effective(project, chapter)
    assert eff["scene"].mode.value == "auto"          # theo cấp 0 của chương
    assert eff["audio"].mode.value == "manual"        # ghi đè cổng của dự án vẫn giữ


def test_state_resolve_and_marks():
    evs = [
        {"id": 1, "chapter_idx": 2, "field": "outfit", "value": "robe", "status": "approved", "until_chapter": None},
        {"id": 2, "chapter_idx": 2, "field": "injury", "value": "bandage", "status": "approved", "until_chapter": 3},
        {"id": 3, "chapter_idx": 3, "field": "mark", "value": "scar on right cheek", "status": "approved", "until_chapter": None},
        {"id": 4, "chapter_idx": 5, "field": "mark", "value": "tattoo on neck", "status": "approved", "until_chapter": None},
        {"id": 5, "chapter_idx": 6, "field": "outfit", "value": "", "status": "approved", "until_chapter": None},
        {"id": 6, "chapter_idx": 4, "field": "hair", "value": "short", "status": "pending", "until_chapter": None},
    ]
    assert st.resolve(evs, 1) == {}
    assert st.resolve(evs, 3) == {"outfit": "robe", "injury": "bandage", "mark": "scar on right cheek"}
    assert st.resolve(evs, 4) == {"outfit": "robe", "mark": "scar on right cheek"}
    assert st.resolve(evs, 5)["mark"] == "scar on right cheek; tattoo on neck"
    assert "outfit" not in st.resolve(evs, 6)
    # sẹo vĩnh viễn đổi ảnh tham chiếu MẶT; vết thương tạm thì không
    assert st.face_key(st.resolve(evs, 2)) == "base"
    assert st.face_key(st.resolve(evs, 3)) != "base"
    assert st.normalize_field("injury", permanent=True) == "mark"
    assert st.normalize_field("scar") == "mark"


def test_location_key_by_variant_and_state():
    assert st.location_key({}, "default") == "default"
    assert st.location_key({}, "night") == "night"
    ruined = st.location_key({"condition": "ruined"}, "night")
    assert ruined.startswith("night-") and ruined != st.location_key({}, "night")


def test_checks_evidence_and_names():
    long_text = (EXAMPLES / "sample_story" / "02.txt").read_text(encoding="utf-8") * 20
    assert checks.evidence_found("Lâm An khoác đạo bào màu lam nhạt của đệ tử ngoại môn", long_text)
    assert not checks.evidence_found("Lâm An rút thanh kiếm sắt đen chém đứt đầu con rồng", long_text)
    assert checks.name_similarity("Lam An", "Lâm An") == 1.0
    assert checks.name_similarity("L. An", "Lâm An") >= 0.9
    assert checks.name_similarity("An", "Lâm An") >= 0.84
    assert checks.name_similarity("Tô Nguyệt", "Lâm An") < 0.5
    flags = checks.event_flags("age", "15 years old", True, "x", "y", {"age": "17 years old"})
    assert "age_regression" in flags


def test_span_issues():
    B = llm.BeatOut
    assert llm.span_issues([B(start=1, end=2), B(start=3, end=5)], 5) == []
    issues = llm.span_issues([B(start=1, end=2), B(start=4, end=5)], 6)
    assert any("bỏ sót đoạn 3" in i for i in issues) and any("6..6" in i for i in issues)
    assert llm.span_issues([B(start=1, end=3), B(start=2, end=4)], 4)


def test_comic_layout_by_shot_and_dialogue():
    beats = [({"id": 1, "shot": "wide"}, []), ({"id": 2, "shot": "medium"}, []), ({"id": 3, "shot": "medium"}, []),
             ({"id": 4, "shot": "close"}, []), ({"id": 5, "shot": "close"}, []), ({"id": 6, "shot": "close"}, []),
             ({"id": 7, "shot": "medium"}, [{"text": "a"}, {"text": "b"}, {"text": "c"}])]
    rows = comic.plan_rows(beats)
    assert [k for k, _ in rows] == ["full", "half", "small", "full"]
    pages = comic.plan_pages(beats, 1600, 2400, 48, 6)
    for pg in pages:
        assert len(pg["panels"]) <= 6
        assert max(p["y"] + p["h"] for p in pg["panels"]) <= 1.0001
    web = comic.plan_webtoon(beats, 1080, 24, 48, 4)
    assert len(web) == 2 and all(p["w"] == 1.0 for pg in web for p in pg["panels"])


def test_balloon_fit_and_overflow():
    font = comic.find_font()
    long = {"text": "Lời thoại rất dài " * 40, "x": .03, "y": .03, "w": .4, "kind": "speech"}
    short = {"text": "Chào", "x": .03, "y": .03, "w": .4, "kind": "speech"}
    assert comic.overflow([long], 300, 200, font, 20)
    assert not comic.overflow([short], 300, 200, font, 20)
    bl = comic.default_balloons({"narration": ""}, [{"text": "A", "character": "Lâm An"}, {"text": "B", "character": "x"}],
                                {checks.norm("LÂM AN"): 7}, 400, 300, font, 18)
    assert bl[0]["character_id"] == 7 and bl[1]["y"] > bl[0]["y"]


def test_video_cues():
    words = [{"start": i * .3, "end": i * .3 + .3, "text": w} for i, w in enumerate("Một hai ba. Bốn năm sáu bảy.".split())]
    cues = video.cues_for_segment("x", 3.0, {"words": words}, 10.0)
    assert len(cues) == 2 and cues[0]["start"] == 10.0 and cues[0]["text"] == "Một hai ba."
    long = "Câu này rất dài, có nhiều vế, cần được tách ra thành nhiều dòng phụ đề ngắn hơn, để người xem dễ đọc hơn."
    cues = video.cues_for_segment(long, 10.0, {}, 0.0, max_chars=40)
    assert len(cues) >= 3 and all(len(c["text"]) <= 40 for c in cues)
    assert abs(cues[-1]["end"] - 10.0) < 1e-6
    assert "00:00:01,500" in video.build_srt([{"start": 1.5, "end": 2, "text": "a"}])


def test_audio_flags():
    assert checks.audio_flags("một hai ba bốn năm", 1.5) == []
    assert checks.audio_flags("một hai ba bốn năm", 0.2) == ["audio_mismatch"]
    assert checks.audio_flags("một hai", 6.0) == ["audio_mismatch"]
