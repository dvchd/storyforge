"""Kiểm thử đơn vị cho phần lõi."""
from __future__ import annotations

import random

from helpers import EXAMPLES  # noqa: F401  (đảm bảo sys.path)

from storyforge.app import checks, comic, llm, policy, state as st, system, validate, video
from storyforge.app.engine import DEFAULT_SETTINGS, _lang_flags, dedupe_fuzzy_on, dedupe_llm_on, default_focus, settings_of
from storyforge.app.routes_web import split_chapters


def test_split_chapters():
    items = split_chapters((EXAMPLES / "sample_story" / "01.txt").read_text(encoding="utf-8"))
    assert len(items) == 1 and items[0][0].startswith("Chương 1")
    assert [t for t, _ in split_chapters("Chương 1\nA\n\nB\nChương 2: X\nC\nHồi 3\nD")] == ["Chương 1", "Chương 2: X", "Hồi 3"]


def test_policy_decide_and_info_flags():
    gp = policy.GatePolicy(mode="auto_if_clean", always_review=["permanent"])
    assert policy.decide(gp, [], random.Random(1)) == "approved"
    assert policy.decide(gp, ["new_identity"], random.Random(1)) == "approved"
    assert policy.decide(gp, ["evidence_missing"], random.Random(1)) == "pending"
    assert policy.decide(gp, ["non_english_prompt"], random.Random(1)) == "pending"
    assert policy.decide(policy.GatePolicy(mode="auto", always_review=["permanent"]), ["permanent"], random.Random(1)) == "pending"


def test_policy_chapter_level_keeps_project_overrides():
    eff = policy.effective({"policy_json": '{"level": "2", "gates": {"audio": {"mode": "manual"}}}'},
                           {"policy_json": '{"level": "0"}'})
    assert eff["scene"].mode.value == "auto" and eff["audio"].mode.value == "manual"


def test_state_marks_accumulate_and_remove_individually():
    E = lambda i, ch, f, v, until=None, status="approved": {"id": i, "chapter_idx": ch, "field": f, "value": v,
                                                           "status": status, "until_chapter": until}
    evs = [E(1, 2, "outfit", "robe"), E(2, 2, "injury", "bandage", 3), E(3, 3, "mark", "scar on right cheek"),
           E(4, 4, "mark", "dragon tattoo on left arm"), E(5, 5, "mark", "-Dragon tattoo on left arm"),
           E(6, 6, "outfit", ""), E(7, 7, "mark", ""), E(8, 4, "hair", "short", status="pending")]
    assert st.resolve(evs, 1) == {}
    assert st.resolve(evs, 3) == {"outfit": "robe", "injury": "bandage", "mark": "scar on right cheek"}
    assert st.marks_of(st.resolve(evs, 4)) == ["scar on right cheek", "dragon tattoo on left arm"]
    assert st.marks_of(st.resolve(evs, 5)) == ["scar on right cheek"], "xóa riêng hình xăm, giữ sẹo"
    assert "outfit" not in st.resolve(evs, 6)
    assert "mark" not in st.resolve(evs, 7), "giá trị rỗng vẫn xóa hết"
    assert st.face_key(st.resolve(evs, 2)) == "base" and st.face_key(st.resolve(evs, 3)) != "base"
    assert st.face_key(st.resolve(evs, 4)) != st.face_key(st.resolve(evs, 5))
    assert st.normalize_field("injury", permanent=True) == "mark"
    cur = st.resolve(evs, 3)
    assert "conflict" in checks.event_flags("mark", "-tattoo on neck", True, "x", "y", cur)
    assert "redundant" in checks.event_flags("mark", "scar on right cheek", True, "x", "y", cur)


def test_location_key_by_variant_and_state():
    assert st.location_key({}, "default") == "default"
    ruined = st.location_key({"condition": "ruined"}, "night")
    assert ruined.startswith("night-") and ruined != st.location_key({}, "night")


def test_name_similarity_strict_for_single_word():
    assert checks.name_similarity("Lam An", "Lâm An") == 1.0
    assert checks.name_similarity("L. An", "Lâm An") >= 0.9
    assert checks.name_similarity("An", "Lâm An") < 0.9, "tên một chữ không tự khớp tên dài"
    assert checks.name_similarity("Minh", "Tiểu Minh") < 0.9
    assert checks.name_similarity("Lâm An", "Lâm An Nhiên") == 0.86
    assert checks.name_similarity("Tô Nguyệt", "Lâm An") < 0.5
    assert DEFAULT_SETTINGS["dedupe_threshold"] == 0.90


def test_evidence_and_age():
    long_text = (EXAMPLES / "sample_story" / "02.txt").read_text(encoding="utf-8") * 20
    assert checks.evidence_found("Lâm An khoác đạo bào màu lam nhạt của đệ tử ngoại môn", long_text)
    assert not checks.evidence_found("Lâm An rút thanh kiếm sắt đen chém đứt đầu con rồng", long_text)
    assert "age_regression" in checks.event_flags("age", "15 years old", True, "x", "y", {"age": "17 years old"})


def test_non_english_detection():
    names = ["Lâm An", "Thanh Vân Tông"]
    assert not checks.non_english("young man with a scar, standing near Thanh Vân Tông", names)
    assert not checks.non_english("distinct look for Lâm An", names)
    assert checks.non_english("đạo bào màu lam nhạt", names)
    assert checks.non_english("áo", names)
    assert not checks.non_english("", names)


def test_span_issues():
    B = llm.BeatOut
    assert llm.span_issues([B(start=1, end=2), B(start=3, end=5)], 5) == []
    issues = llm.span_issues([B(start=1, end=2), B(start=4, end=5)], 6)
    assert any("bỏ sót đoạn 3" in i for i in issues) and any("6..6" in i for i in issues)


def test_comic_layout_and_crop():
    beats = [({"id": 1, "shot": "wide"}, []), ({"id": 2, "shot": "medium"}, []), ({"id": 3, "shot": "medium"}, []),
             ({"id": 4, "shot": "close"}, []), ({"id": 5, "shot": "close"}, []), ({"id": 6, "shot": "close"}, []),
             ({"id": 7, "shot": "medium"}, [{"text": "a"}, {"text": "b"}, {"text": "c"}])]
    assert [k for k, _ in comic.plan_rows(beats)] == ["full", "half", "small", "full"]
    for pg in comic.plan_pages(beats, 1600, 2400, 48, 6):
        assert len(pg["panels"]) <= 6 and max(p["y"] + p["h"] for p in pg["panels"]) <= 1.0001
    # cắt khung từ ảnh gốc 1.32:1 ra 16:9 và 1:1
    v = comic.crop_box(1320, 1000, 16 / 9, .5, .35)
    c = comic.crop_box(1320, 1000, 1.0, .5, .35)
    assert abs(v[2] - 1.0) < 1e-9 and v[3] < 1 and abs(c[3] - 1.0) < 1e-9 and c[2] < 1
    assert comic.overlap(v, c) > 0.6
    far = comic.crop_box(2000, 1000, 1.0, 0.0, .5), comic.crop_box(2000, 1000, 1.0, 1.0, .5)
    assert comic.overlap(*far) < 0.6
    assert default_focus("close")[1] < default_focus("wide")[1]


def test_balloon_fit_and_overflow():
    font = comic.find_font()
    assert comic.overflow([{"text": "Lời thoại rất dài " * 40, "x": .03, "y": .03, "w": .4, "kind": "speech"}], 300, 200, font, 20)
    assert not comic.overflow([{"text": "Chào", "x": .03, "y": .03, "w": .4, "kind": "speech"}], 300, 200, font, 20)
    bl = comic.default_balloons({"narration": ""}, [{"text": "A", "character": "Lâm An"}, {"text": "B", "character": "x"}],
                                {checks.norm("LÂM AN"): 7}, 400, 300, font, 18)
    assert bl[0]["character_id"] == 7 and bl[1]["y"] > bl[0]["y"]


def test_video_cues():
    words = [{"start": i * .3, "end": i * .3 + .3, "text": w} for i, w in enumerate("Một hai ba. Bốn năm sáu bảy.".split())]
    cues = video.cues_for_segment("x", 3.0, {"words": words}, 10.0)
    assert len(cues) == 2 and cues[0]["start"] == 10.0 and cues[0]["text"] == "Một hai ba."
    long = "Câu này rất dài, có nhiều vế, cần được tách ra thành nhiều dòng phụ đề ngắn hơn, để người xem dễ đọc hơn."
    cues = video.cues_for_segment(long, 10.0, {}, 0.0, max_chars=40)
    assert len(cues) >= 3 and all(len(c["text"]) <= 40 for c in cues) and abs(cues[-1]["end"] - 10.0) < 1e-6


def test_audio_flags():
    assert checks.audio_flags("một hai ba bốn năm", 1.5) == []
    assert checks.audio_flags("một hai ba bốn năm", 0.2) == ["audio_mismatch"]
    assert checks.audio_flags("một hai", 6.0) == ["audio_mismatch"]


def test_settings_validation():
    s = dict(DEFAULT_SETTINGS)
    both = {"mode": "both"}
    need = validate.required_image_area(both, s)
    assert need["required"] > 1344 * 768 and need["recommended"] >= need["required"]
    small = {**s, "image_area": 300_000}
    assert any(w["key"] == "image_area" and w["level"] == "warn" for w in validate.warnings(both, small))
    big = {**s, "image_area": need["recommended"] * 5}
    assert any(w["key"] == "image_area" and w["level"] == "info" for w in validate.warnings(both, big))
    ok = {**s, "image_area": need["recommended"]}
    assert not [w for w in validate.warnings(both, ok) if w["key"] == "image_area"]
    fixed = validate.fix({"video_width": 1281, "video_height": 721, "comic_max_panels": 40, "beats_span_retries": 9})
    assert fixed == {"video_width": 1282, "video_height": 722, "comic_max_panels": 9, "beats_span_retries": 2}
    assert any(w["key"] == "dedupe_threshold" for w in validate.warnings(both, {**ok, "dedupe_threshold": 0.8}))


def test_crop_intersection_box():
    v = comic.crop_box(1320, 1000, 16 / 9, .5, .35)
    c = comic.crop_box(1320, 1000, 1.0, .5, .35)
    inter = comic.intersect(v, c)
    assert inter is not None and inter[2] > 0 and inter[3] > 0
    assert inter[0] >= min(v[0], c[0]) - 1e-9 and inter[0] + inter[2] <= max(v[0] + v[2], c[0] + c[2]) + 1e-9
    far = comic.crop_box(2000, 1000, 1.0, 0.0, .5), comic.crop_box(2000, 1000, 1.0, 1.0, .5)
    assert comic.intersect(*far) is None, "hai khung chỉ chạm mép thì coi như không giao"


def test_stall_thresholds_split_by_kind():
    from storyforge.app.jobs import DEFAULT_STALL
    assert DEFAULT_STALL["image.generate"] >= 1200, "ảnh máy yếu cần chờ lâu"
    assert DEFAULT_STALL["tts.synthesize"] <= 300, "giọng đọc phải thu hồi sớm"
    assert DEFAULT_STALL["llm.chat"] == 0


def test_dedupe_toggles_with_legacy_fallback():
    assert dedupe_fuzzy_on(settings_of({"settings_json": "{}"}))
    assert dedupe_llm_on(settings_of({"settings_json": "{}"}))
    legacy = settings_of({"settings_json": '{"dedupe_mode": "llm_only"}'})
    assert not dedupe_fuzzy_on(legacy) and dedupe_llm_on(legacy)
    off = settings_of({"settings_json": '{"dedupe_mode": "off"}'})
    assert not dedupe_fuzzy_on(off) and not dedupe_llm_on(off)


def test_english_allow_list():
    assert checks.non_english("áo dài đỏ thêu hoa", [])
    p = {"settings": {"check_english": True, "english_allow": "áo dài đỏ thêu hoa"}}
    assert _lang_flags(p, ["áo dài đỏ thêu hoa"], []) == []
    p2 = {"settings": {"check_english": True, "english_allow": ""}}
    assert _lang_flags(p2, ["áo dài đỏ thêu hoa"], []) == ["non_english_prompt"]


def test_doctor_port_check():
    import socket

    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.bind(("127.0.0.1", 0))
    busy = sk.getsockname()[1]
    try:
        assert not system.port_free("127.0.0.1", busy)
    finally:
        sk.close()
    assert system.port_free("127.0.0.1", busy)
