"""Engine điều phối quy trình.

Luồng chung:  chương -> trích trạng thái -> duyệt -> chia nhịp -> duyệt -> sản xuất -> xuất bản
Sản xuất:     ảnh tham chiếu (mặt, toàn thân, bối cảnh theo biến thể và hiện trạng)
              -> MỘT ảnh gốc cho mỗi nhịp, dùng chung cho video (cắt 16:9) và truyện tranh (cắt theo khung)
              -> giọng đọc từng đoạn
Xuất bản:     dựng video (ffmpeg) / ghép trang truyện tranh (Pillow)

Engine chỉ tạo job và xử lý kết quả. Mọi mục đều qua cổng duyệt (policy.decide).
"""
from __future__ import annotations

import logging
import math
import random
import shutil
from pathlib import Path

from pydantic import ValidationError

from storyforge.protocol import ImageGeneratePayload, LlmChatPayload, RefImage, TtsPayload

from . import assets, checks, comic, jobs, llm, policy, prompts, state as st, system, validate, video
from .config import get_settings
from .db import db, jd, jl, now

log = logging.getLogger("storyforge.engine")

DEFAULT_SETTINGS: dict = {
    # LLM
    "llm_model": "", "llm_fallback_model": "", "llm_extra": "", "llm_temperature": 0.2, "llm_max_tokens": None,
    "beats_span_retries": 1,       # số lần bắt LLM chia lại khi sai khoảng đoạn; hết lượt thì tự sửa + gắn cờ
    "context_chapters": 3,
    "dedupe_mode": "fuzzy",        # fuzzy: gợi ý theo tên gần giống | llm_only: chỉ khi bấm "Nhờ LLM rà" | off
    "dedupe_threshold": 0.90,
    "check_english": True,
    # Ảnh
    "image_model": "", "image_steps": None, "negative_prompt": "", "image_area": 1048576, "ref_size": 1024,
    "max_cast_refs": 2, "max_refs": 4, "crop_overlap_min": 0.6,
    # TTS
    "tts_model": "", "tts_voice": "", "tts_rate": 1.0, "language": "vi",
    # Video
    "video_width": 1344, "video_height": 768, "video_fps": 30, "video_encoder": "auto", "video_zoom": 0.10,
    "video_transition": 0.3, "video_gap": 0.35, "video_loudnorm": True,
    # Truyện tranh
    "comic_format": "page",
    "comic_page_width": 1600, "comic_page_height": 2400, "comic_max_panels": 6,
    "comic_margin": 48, "comic_gutter": 24, "webtoon_width": 1080, "webtoon_panels_per_page": 8,
    # Duyệt
    "auto_regen_on_reject": True, "max_regen": 3,
}

# Điểm lấy nét mặc định theo góc máy: cận mặt lấy cao hơn để không cắt mất đầu.
SHOT_FOCUS = {"close": (0.5, 0.35), "medium": (0.5, 0.42), "wide": (0.5, 0.5)}

STATUS_ORDER = ["new", "extracting", "review_state", "state_ready", "beating", "review_beats", "producing", "done"]
STATUS_LABELS = {
    "new": "Mới", "extracting": "Đang trích trạng thái", "review_state": "Chờ duyệt trạng thái",
    "state_ready": "Trạng thái xong", "beating": "Đang chia nhịp", "review_beats": "Chờ duyệt nhịp",
    "producing": "Đang sản xuất", "done": "Hoàn tất", "error": "Lỗi",
}


def rank(s: str) -> int:
    return STATUS_ORDER.index(s) if s in STATUS_ORDER else -1


# ================================================================ cơ bản
def project(pid: int) -> dict:
    p = db.get("project", pid)
    if not p:
        raise KeyError(f"project {pid}")
    p["settings"] = settings_of(p)
    return p


def settings_of(p: dict) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(jl(p.get("settings_json"), {}) or {})
    return s


def wants_video(p: dict) -> bool:
    return p["mode"] in ("video", "both")


def wants_comic(p: dict) -> bool:
    return p["mode"] in ("comic", "both")


def audit(pid: int, entity: str, eid: int, action: str, reviewer: str, note: str = "") -> None:
    db.insert("audit", project_id=pid, entity=entity, entity_id=eid, action=action, reviewer=reviewer,
              note=note, created_at=now())


def gate_status(p: dict, ch: dict | None, gate: str, flags: list[str], key: str) -> tuple[str, str]:
    gp = policy.effective(p, ch)[gate]
    status = policy.decide(gp, set(flags), random.Random(f"{p['id']}:{gate}:{key}"))
    return status, (policy.policy_name(p, ch, gate) if status == "approved" else "")


def gate_batch(p: dict, ch: dict | None, gate: str) -> str:
    return policy.effective(p, ch)[gate].batch


# ================================================================ nhân vật, bối cảnh
def all_chars(pid: int, include_rejected: bool = False) -> list[dict]:
    sql = "SELECT * FROM character WHERE project_id=?" + ("" if include_rejected else " AND status!='rejected'")
    rows = db.q(sql + " ORDER BY first_chapter, id", pid)
    for r in rows:
        r["aliases"] = jl(r["aliases_json"], [])
    return rows


def locs(pid: int, include_rejected: bool = False) -> list[dict]:
    sql = "SELECT * FROM location WHERE project_id=?" + ("" if include_rejected else " AND status!='rejected'")
    rows = db.q(sql + " ORDER BY first_chapter, id", pid)
    for r in rows:
        r["aliases"] = jl(r["aliases_json"], [])
    return rows


def _index(rows: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for r in rows:
        for n in [r["name"], *r.get("aliases", [])]:
            if n:
                idx.setdefault(checks.norm(n), r)
    return idx


def name_index(pid: int) -> dict[str, dict]:
    return _index(all_chars(pid))


def loc_index(pid: int) -> dict[str, dict]:
    return _index(locs(pid))


def proper_names(pid: int) -> list[str]:
    """Tên riêng đã biết: bỏ qua khi kiểm tra mô tả có phải tiếng Anh không."""
    return [n for r in all_chars(pid, True) + locs(pid, True) for n in [r["name"], *r["aliases"]]]


def resolve_name(idx: dict[str, dict], name: str, threshold: float) -> tuple[dict | None, bool]:
    """Khớp chuẩn hóa, rồi khớp gần đúng (chỉ khi rất giống). Trả (bản ghi, có phải gần đúng không)."""
    key = checks.norm(name)
    if key in idx:
        return idx[key], False
    cid, score = checks.best_match(name, {k: i for i, (k, _) in enumerate(idx.items())})
    if cid is not None and score >= max(threshold, 0.9):
        return list(idx.values())[cid], True
    return None, False


def chars_context(pid: int, chapter_idx: int) -> list[dict]:
    return [{**c, "state": st.state_at(c["id"], chapter_idx)} for c in all_chars(pid) if c["first_chapter"] <= chapter_idx]


def locs_context(pid: int, chapter_idx: int) -> list[dict]:
    return [{**l, "state": st.loc_state_at(l["id"], chapter_idx)} for l in locs(pid) if l["first_chapter"] <= chapter_idx]


def add_suggestion(pid: int, kind: str, keep_id: int, merge_id: int, score: float, reason: str, source: str) -> None:
    if keep_id == merge_id:
        return
    if db.one("SELECT id FROM suggestion WHERE kind=? AND ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?))",
              kind, keep_id, merge_id, merge_id, keep_id):
        return
    db.insert("suggestion", project_id=pid, kind=kind, a_id=keep_id, b_id=merge_id, score=score, reason=reason,
              source=source, status="open", created_at=now())


def _identity_flags(p: dict, name: str, aliases: list[str], existing: list[dict]) -> tuple[list[str], list[str], list]:
    s = p["settings"]
    flags: list[str] = []
    idx = _index(existing)
    clean = []
    for a in aliases:
        if checks.norm(a) in idx:
            flags.append("alias_collision")
        elif a.strip():
            clean.append(a.strip())
    dups = []
    if s.get("dedupe_mode", "fuzzy") == "fuzzy":
        thr = float(s["dedupe_threshold"])
        for r in existing:
            score = max(checks.name_similarity(n, m) for n in [name, *clean] for m in [r["name"], *r.get("aliases", [])])
            if score >= thr:
                dups.append((r["id"], score))
        if dups:
            flags.append("possible_duplicate")
    return flags, clean, dups


def _lang_flags(p: dict, texts: list[str], names: list[str]) -> list[str]:
    if not p["settings"].get("check_english", True):
        return []
    return ["non_english_prompt"] if any(checks.non_english(t, names) for t in texts if t) else []


def create_character(p: dict, ch: dict, name: str, aliases: list[str], appearance: str, role: str,
                     extra_flags: list[str] | None = None) -> dict:
    flags, aliases, dups = _identity_flags(p, name, aliases, all_chars(p["id"]))
    flags += _lang_flags(p, [appearance], proper_names(p["id"]) + [name, *aliases])
    flags = sorted(set(["new_identity", *flags, *(extra_flags or [])]))
    status, reviewer = gate_status(p, ch, "identity", flags, f"char:{name}")
    cid = db.insert("character", project_id=p["id"], name=name.strip(), aliases_json=jd(aliases),
                    appearance=appearance or "", role=role or "", first_chapter=ch["idx"], status=status,
                    flags_json=jd(flags), reviewer=reviewer, created_at=now())
    for other, score in dups:
        add_suggestion(p["id"], "merge_character", other, cid, score, "Tên gần giống", "fuzzy")
    if status == "approved":
        audit(p["id"], "character", cid, "approve", reviewer)
    c = db.get("character", cid)
    c["aliases"] = aliases
    return c


def create_location(p: dict, ch: dict, name: str, aliases: list[str], description: str,
                    extra_flags: list[str] | None = None) -> dict:
    flags, aliases, dups = _identity_flags(p, name, aliases, locs(p["id"]))
    flags += _lang_flags(p, [description], proper_names(p["id"]) + [name, *aliases])
    flags = sorted(set(["new_identity", *flags, *(extra_flags or [])]))
    status, reviewer = gate_status(p, ch, "identity", flags, f"loc:{name}")
    lid = db.insert("location", project_id=p["id"], name=name.strip(), aliases_json=jd(aliases),
                    description=description or "", first_chapter=ch["idx"], status=status,
                    flags_json=jd(flags), reviewer=reviewer, created_at=now())
    for other, score in dups:
        add_suggestion(p["id"], "merge_location", other, lid, score, "Tên gần giống", "fuzzy")
    if status == "approved":
        audit(p["id"], "location", lid, "approve", reviewer)
    l = db.get("location", lid)
    l["aliases"] = aliases
    return l


# ================================================================ trích trạng thái
def _llm_payload(p: dict, msgs: list[dict], schema: type, name: str, meta: dict) -> dict:
    s = p["settings"]
    return LlmChatPayload(messages=msgs, json_schema=llm.schema_of(schema), schema_name=name,
                          temperature=float(s["llm_temperature"]), max_tokens=s.get("llm_max_tokens"),
                          meta={"base_messages": len(msgs), **meta}).model_dump()


def enqueue_extract(p: dict, ch: dict) -> None:
    s = p["settings"]
    prev = db.q("SELECT idx, summary FROM chapter WHERE project_id=? AND idx<? ORDER BY idx DESC LIMIT ?",
                p["id"], ch["idx"], int(s["context_chapters"]))[::-1]
    chars = chars_context(p["id"], ch["idx"] - 1)
    lc = locs_context(p["id"], ch["idx"] - 1)
    msgs = llm.extract_messages(p, ch, chars, lc, prev, st.describe, s.get("llm_extra", ""))
    payload = _llm_payload(p, msgs, llm.ChapterExtraction, "ChapterExtraction", {
        "task": "extract", "chapter_text": ch["text"],
        "known_characters": [{"name": c["name"], "aliases": c["aliases"]} for c in chars],
        "known_locations": [{"name": l["name"], "aliases": l["aliases"]} for l in lc]})
    jobs.enqueue(p["id"], "llm.chat", payload, "chapter_extract", ch["id"], model_hint=s["llm_model"], priority=5)
    db.update("chapter", ch["id"], status="extracting", error="")


def _retry_llm(job: dict, p: dict, ch: dict, text: str, err: str) -> None:
    if not jobs.retry_with_feedback(job, text, err, p["settings"].get("llm_fallback_model", "")):
        db.update("chapter", ch["id"], status="error", error=f"LLM trả kết quả không hợp lệ: {err}"[:2000])


def _event(p: dict, ch: dict, subject_type: str, subject_id: int, field: str, value: str, permanent: bool,
           lasts: int | None, evidence: str, current: dict, extra_flags: list[str], names: list[str]) -> tuple[int, str, str]:
    if subject_type == "character":
        flags = checks.event_flags(field, value, permanent, evidence, ch["text"], current)
    else:
        flags = [] if checks.evidence_found(evidence, ch["text"]) else ["evidence_missing"]
        if permanent:
            flags.append("permanent")
    flags += _lang_flags(p, [st.removal_target(value)], names)
    flags = sorted(set(flags + extra_flags))
    if field == "mark":
        permanent = True
    until = ch["idx"] + lasts - 1 if lasts and not permanent else None
    status, reviewer = gate_status(p, ch, "state", flags, f"ev:{ch['id']}:{subject_type}:{subject_id}:{field}:{value}")
    eid = db.insert("state_event", project_id=p["id"], subject_type=subject_type, subject_id=subject_id,
                    chapter_id=ch["id"], chapter_idx=ch["idx"], field=field, value=(value or "").strip(),
                    permanent=int(permanent), until_chapter=until, evidence=evidence, status=status,
                    flags_json=jd(flags), reviewer=reviewer, created_at=now())
    return eid, status, reviewer


def handle_extract(job: dict, output: dict) -> None:
    ch = db.get("chapter", job["owner_id"])
    if not ch:
        return
    p = project(ch["project_id"])
    thr = float(p["settings"]["dedupe_threshold"])
    text = output.get("text", "")
    try:
        data = llm.ChapterExtraction.model_validate(llm.extract_json(text))
    except (ValueError, ValidationError) as e:
        _retry_llm(job, p, ch, text, str(e))
        return
    with db.tx():
        db.ex("DELETE FROM state_event WHERE chapter_id=? AND status!='approved'", ch["id"])
        cidx = name_index(p["id"])
        for nc in data.new_characters:
            if not checks.norm(nc.name):
                continue
            c, fuzzy = resolve_name(cidx, nc.name, thr)
            if c is not None and not fuzzy:
                merged = list(dict.fromkeys([*c["aliases"], *[a for a in nc.aliases if checks.norm(a) not in cidx]]))
                if merged != c["aliases"]:
                    db.update("character", c["id"], aliases_json=jd(merged))
                    c["aliases"] = merged
                if not c["appearance"] and nc.appearance:
                    db.update("character", c["id"], appearance=nc.appearance)
                continue
            c = create_character(p, ch, nc.name, nc.aliases, nc.appearance, nc.role)
            for n in [c["name"], *c["aliases"]]:
                cidx.setdefault(checks.norm(n), c)
        lidx = loc_index(p["id"])
        for nl in data.locations:
            if not checks.norm(nl.name):
                continue
            l, fuzzy = resolve_name(lidx, nl.name, thr)
            if l is None or fuzzy:
                l = create_location(p, ch, nl.name, nl.aliases, nl.description)
                for n in [l["name"], *l["aliases"]]:
                    lidx.setdefault(checks.norm(n), l)
        names = proper_names(p["id"])
        created: list[tuple[int, str, str]] = []
        for ev in data.events:
            c, fuzzy = resolve_name(cidx, ev.character, thr)
            extra = ["unknown_character"] if fuzzy else []
            if c is None:
                c = create_character(p, ch, ev.character, [], "", "", ["unknown_character"])
                cidx[checks.norm(ev.character)] = c
                extra = ["unknown_character"]
            field = st.normalize_field(ev.field, ev.permanent)
            created.append(_event(p, ch, "character", c["id"], field, ev.value, ev.permanent, ev.lasts_chapters,
                                  ev.evidence, st.state_at(c["id"], ch["idx"] - 1), extra, names))
        for ev in data.location_events:
            l, fuzzy = resolve_name(lidx, ev.location, thr)
            if l is None:
                l = create_location(p, ch, ev.location, [], "", ["unknown_location"])
                lidx[checks.norm(ev.location)] = l
            field = st.normalize_field(ev.field, ev.permanent, "location")
            created.append(_event(p, ch, "location", l["id"], field, ev.value, ev.permanent, ev.lasts_chapters,
                                  ev.evidence, st.loc_state_at(l["id"], ch["idx"] - 1),
                                  ["unknown_location"] if fuzzy else [], names))
        if gate_batch(p, ch, "state") == "chapter" and any(s != "approved" for _, s, _ in created):
            db.ex("UPDATE state_event SET status='pending', reviewer='' WHERE chapter_id=?", ch["id"])
        else:
            for eid, s, r in created:
                if s == "approved":
                    audit(p["id"], "event", eid, "approve", r)
        db.update("chapter", ch["id"], summary=data.summary.strip(), status="review_state", needs_recheck=0)
    advance_chapter(ch["id"])


def pending_identity(ch: dict) -> int:
    return (db.val("SELECT COUNT(*) FROM character WHERE project_id=? AND first_chapter<=? AND status='pending'",
                   ch["project_id"], ch["idx"]) or 0) + \
           (db.val("SELECT COUNT(*) FROM location WHERE project_id=? AND first_chapter<=? AND status='pending'",
                   ch["project_id"], ch["idx"]) or 0)


def pending_state(ch: dict) -> int:
    return pending_identity(ch) + (db.val(
        "SELECT COUNT(*) FROM state_event WHERE chapter_id=? AND status='pending'", ch["id"]) or 0)


# ================================================================ chia nhịp
def enqueue_beats(p: dict, ch: dict) -> None:
    s = p["settings"]
    paras = llm.paragraphs(ch["text"])
    chars = chars_context(p["id"], ch["idx"])
    lc = locs_context(p["id"], ch["idx"])
    msgs = llm.beats_messages(p, ch, paras, chars, lc, st.describe, s.get("llm_extra", ""))
    payload = _llm_payload(p, msgs, llm.BeatsOut, "BeatsOut", {
        "task": "beats", "paragraphs": paras, "span_retries": 0,
        "known_characters": [{"name": c["name"], "aliases": c["aliases"]} for c in chars],
        "known_locations": [{"name": l["name"], "aliases": l["aliases"]} for l in lc]})
    jobs.enqueue(p["id"], "llm.chat", payload, "chapter_beats", ch["id"], model_hint=s["llm_model"], priority=4)
    db.update("chapter", ch["id"], status="beating", error="")


def _normalize_spans(beats: list[llm.BeatOut], n: int) -> list[tuple[int, tuple[int, int], bool]]:
    out, nxt = [], 1
    ordered = sorted(enumerate(beats), key=lambda t: (t[1].start, t[1].end))
    for i, (orig_i, b) in enumerate(ordered):
        if nxt > n:
            break
        s, e = b.start, b.end
        adj = False
        if s != nxt:
            s, adj = nxt, True
        e2 = max(s, min(e, n))
        if i == len(ordered) - 1 and e2 != n:
            e2 = n
        adj = adj or e2 != e
        out.append((orig_i, (s, e2), adj))
        nxt = e2 + 1
    return out


def handle_beats(job: dict, output: dict) -> None:
    ch = db.get("chapter", job["owner_id"])
    if not ch:
        return
    p = project(ch["project_id"])
    s = p["settings"]
    thr = float(s["dedupe_threshold"])
    text = output.get("text", "")
    paras = llm.paragraphs(ch["text"])
    try:
        data = llm.BeatsOut.model_validate(llm.extract_json(text))
        if not data.beats:
            raise ValueError("Danh sách beats rỗng")
    except (ValueError, ValidationError) as e:
        _retry_llm(job, p, ch, text, str(e))
        return
    issues = llm.span_issues(data.beats, len(paras))
    used = int(jl(job["payload_json"], {}).get("meta", {}).get("span_retries", 0))
    # Chỉ bắt LLM chia lại số lần giới hạn (mặc định 1): LLM yếu thường lặp lại đúng lỗi cũ, thử tiếp chỉ tốn token.
    if issues and used < int(s.get("beats_span_retries", 1)):
        msg = (f"Khoảng đoạn văn sai (chương có {len(paras)} đoạn, phải phủ kín 1..{len(paras)}, liên tiếp, "
               f"không chồng lấn):\n- " + "\n- ".join(issues[:12]))
        if jobs.retry_with_feedback(job, text, msg, s.get("llm_fallback_model", ""), {"span_retries": used + 1}):
            return
    spans = _normalize_spans(data.beats, len(paras))
    names = proper_names(p["id"])
    with db.tx():
        db.ex("DELETE FROM beat WHERE chapter_id=?", ch["id"])
        db.ex("DELETE FROM page WHERE chapter_id=?", ch["id"])
        cidx, lidx = name_index(p["id"]), loc_index(p["id"])
        created = []
        for k, (orig_i, (a, b_end), adj) in enumerate(spans):
            b = data.beats[orig_i]
            flags = ["span_adjusted"] if adj else []
            loc_id = None
            if b.location.strip():
                loc, _ = resolve_name(lidx, b.location, thr)
                if loc is None:
                    loc = create_location(p, ch, b.location, [], b.action, ["unknown_location"])
                    lidx[checks.norm(b.location)] = loc
                    flags.append("unknown_location")
                loc_id = loc["id"]
            else:
                flags.append("no_location")
            cast = []
            for ci in b.cast[:3]:
                c, _ = resolve_name(cidx, ci.character, thr)
                if c is None:
                    c = create_character(p, ch, ci.character, [], "", "", ["unknown_character"])
                    cidx[checks.norm(ci.character)] = c
                    flags.append("unknown_character")
                if all(x["character_id"] != c["id"] for x in cast):
                    cast.append({"character_id": c["id"], "name": c["name"], "pose": ci.pose, "expression": ci.expression})
            if len(b.cast) > 3:
                flags.append("too_many_cast")
            flags += _lang_flags(p, [b.action, b.mood] + [f"{c.pose} {c.expression}" for c in b.cast], names)
            dialogue = [{"character": d.character, "text": d.text, "kind": d.kind} for d in b.dialogue]
            status, reviewer = gate_status(p, ch, "breakdown", flags, f"beat:{ch['id']}:{k}")
            bid = db.insert("beat", project_id=p["id"], chapter_id=ch["id"], idx=k, para_start=a, para_end=b_end,
                            narration="\n\n".join(paras[a - 1:b_end]), location_id=loc_id,
                            variant=(b.variant or "default").strip().lower(), cast_json=jd(cast), shot=b.shot,
                            action=b.action, mood=b.mood, dialogue_json=jd(dialogue), status=status,
                            flags_json=jd(sorted(set(flags))), reviewer=reviewer,
                            seed=prompts.seed_for("beat", ch["id"], k, b.action))
            created.append((bid, status, reviewer))
        if gate_batch(p, ch, "breakdown") == "chapter" and any(x != "approved" for _, x, _ in created):
            db.ex("UPDATE beat SET status='pending', reviewer='' WHERE chapter_id=?", ch["id"])
        else:
            for bid, x, r in created:
                if x == "approved":
                    audit(p["id"], "beat", bid, "approve", r)
        db.update("chapter", ch["id"], status="review_beats")
    advance_chapter(ch["id"])


def pending_beats(ch: dict) -> int:
    return (db.val("SELECT COUNT(*) FROM beat WHERE chapter_id=? AND status='pending'", ch["id"]) or 0) + pending_identity(ch)


# ================================================================ khung sản xuất
def _round64(v: float) -> int:
    return max(256, int(round(v / 64)) * 64)


def master_size(p: dict, panel_aspect: float | None) -> tuple[int, int]:
    s = p["settings"]
    va = int(s["video_width"]) / int(s["video_height"])
    if wants_video(p) and wants_comic(p) and panel_aspect:
        aspect = math.sqrt(va * panel_aspect)
    elif wants_comic(p) and panel_aspect:
        aspect = panel_aspect
    else:
        aspect = va
    aspect = max(0.55, min(1.95, aspect))
    area = int(s["image_area"])
    w = math.sqrt(area * aspect)
    return _round64(w), _round64(area / w)


def comic_geometry(p: dict) -> dict:
    s = p["settings"]
    if s["comic_format"] == "webtoon":
        return {"format": "webtoon", "width": int(s["webtoon_width"]), "margin": int(s["comic_margin"]) // 2,
                "gutter": int(s["comic_gutter"]) * 2}
    return {"format": "page", "width": int(s["comic_page_width"]), "height": int(s["comic_page_height"]),
            "margin": int(s["comic_margin"]), "gutter": int(s["comic_gutter"])}


def base_font(page_w: int) -> int:
    return max(16, page_w // 50)


def default_focus(shot: str) -> tuple[float, float]:
    return SHOT_FOCUS.get(shot or "medium", SHOT_FOCUS["medium"])


def materialize(p: dict, ch: dict) -> None:
    s = p["settings"]
    beats = db.q("SELECT * FROM beat WHERE chapter_id=? AND status='approved' ORDER BY idx", ch["id"])
    aspects: dict[int, float] = {}
    with db.tx():
        if wants_comic(p) and not db.val("SELECT COUNT(*) FROM page WHERE chapter_id=?", ch["id"]):
            g = comic_geometry(p)
            pairs = [(b, jl(b["dialogue_json"], [])) for b in beats]
            if g["format"] == "webtoon":
                pages = comic.plan_webtoon(pairs, g["width"], g["margin"], g["gutter"], int(s["webtoon_panels_per_page"]))
            else:
                pages = comic.plan_pages(pairs, g["width"], g["height"], g["margin"], int(s["comic_max_panels"]))
            char_ids = {checks.norm(n): c["id"] for c in all_chars(p["id"]) for n in [c["name"], *c["aliases"]]}
            font = comic.find_font(get_settings().font_path)
            for pi, pg in enumerate(pages):
                page_id = db.insert("page", project_id=p["id"], chapter_id=ch["id"], idx=pi,
                                    width=pg["width"], height=pg["height"])
                bf = base_font(pg["width"])
                for slot, pn in enumerate(pg["panels"]):
                    b = pn["beat"]
                    _, _, w, h = comic.panel_px(pn, pg["width"], pg["height"], g["margin"], g["gutter"])
                    aspects[b["id"]] = w / max(1, h)
                    balloons = comic.default_balloons(b, jl(b["dialogue_json"], []), char_ids, w, h, font, bf)
                    flags = ["text_overflow"] if comic.overflow(balloons, w, h, font, bf) else []
                    fx, fy = default_focus(b["shot"])
                    pid_ = db.insert("panel", project_id=p["id"], chapter_id=ch["id"], page_id=page_id, beat_id=b["id"],
                                     slot=slot, x=pn["x"], y=pn["y"], w=pn["w"], h=pn["h"], focus_x=fx, focus_y=fy,
                                     flags_json=jd(flags))
                    for bi, bl in enumerate(balloons):
                        db.insert("balloon", panel_id=pid_, idx=bi, **bl)
        if wants_video(p) and not db.val("SELECT COUNT(*) FROM segment WHERE chapter_id=?", ch["id"]):
            for i, b in enumerate(beats):
                fx, fy = default_focus(b["shot"])
                db.insert("segment", project_id=p["id"], chapter_id=ch["id"], beat_id=b["id"], idx=i,
                          narration=b["narration"], focus_x=fx, focus_y=fy)
        for b in beats:
            if not b["img_width"]:
                w, h = master_size(p, aspects.get(b["id"]))
                db.update("beat", b["id"], img_width=w, img_height=h)


def panel_aspect(p: dict, panel: dict) -> float:
    pg = db.get("page", panel["page_id"])
    g = comic_geometry(p)
    _, _, w, h = comic.panel_px(panel, pg["width"], pg["height"], g["margin"], g["gutter"])
    return w / max(1, h)


def crop_info(p: dict, beat: dict, seg: dict | None, panel: dict | None) -> dict:
    """Hai vùng cắt video / truyện trên ảnh gốc có lệch nhau quá ngưỡng không."""
    if not (seg and panel and beat["img_width"]):
        return {"mismatch": False}
    s = p["settings"]
    va = int(s["video_width"]) / int(s["video_height"])
    a = comic.crop_box(beat["img_width"], beat["img_height"], va, seg["focus_x"], seg["focus_y"])
    b = comic.crop_box(beat["img_width"], beat["img_height"], panel_aspect(p, panel), panel["focus_x"], panel["focus_y"])
    ov = comic.overlap(a, b)
    return {"mismatch": ov < float(s["crop_overlap_min"]), "overlap": round(ov, 2), "video_box": a, "panel_box": b}


def crop_mismatches(p: dict, chapter_id: int | None = None) -> int:
    if not (wants_video(p) and wants_comic(p)):
        return 0
    sql = ("SELECT b.*, s.id sid, s.focus_x sfx, s.focus_y sfy, pn.id pnid FROM beat b JOIN segment s ON s.beat_id=b.id "
           "JOIN panel pn ON pn.beat_id=b.id WHERE b.project_id=?")
    args: list = [p["id"]]
    if chapter_id:
        sql += " AND b.chapter_id=?"
        args.append(chapter_id)
    n = 0
    for r in db.q(sql, *args):
        if crop_info(p, r, {"focus_x": r["sfx"], "focus_y": r["sfy"]}, db.get("panel", r["pnid"]))["mismatch"]:
            n += 1
    return n


# ================================================================ ảnh tham chiếu
def _ref_row(owner_type: str, owner_id: int, key: str) -> dict | None:
    return db.one("SELECT * FROM ref WHERE owner_type=? AND owner_id=? AND state_key=?", owner_type, owner_id, key)


def _ref_pairs(r: dict) -> list[tuple[int, str]]:
    out = []
    for x in jl(r["refs_json"], []):
        out.append((int(x[0]), str(x[1])) if isinstance(x, list) else (int(x), "reference"))
    return out


def ensure_ref(p: dict, owner_type: str, owner_id: int, key: str, label: str, prompt: str,
               refs: list[tuple[int, str]], width: int, height: int, chapter_idx: int) -> dict:
    s = p["settings"]
    r = _ref_row(owner_type, owner_id, key)
    ref_ids = [a for a, _ in refs]
    refs_store = jd([[a, role] for a, role in refs])
    if r is None:
        rid = db.insert("ref", project_id=p["id"], owner_type=owner_type, owner_id=owner_id, state_key=key,
                        label=label, prompt=prompt, refs_json=refs_store, width=width, height=height,
                        seed=prompts.seed_for(owner_type, owner_id, key), status="missing",
                        first_chapter=chapter_idx, created_at=now())
        r = db.get("ref", rid)
    elif not r["locked"] and (r["prompt"] != prompt or [a for a, _ in _ref_pairs(r)] != ref_ids) \
            and r["status"] != "generating":
        db.update("ref", r["id"], prompt=prompt, label=label, refs_json=refs_store, status="missing")
        r = db.get("ref", r["id"])
    if chapter_idx < r["first_chapter"]:
        db.update("ref", r["id"], first_chapter=chapter_idx)
    if r["status"] != "missing":
        return r
    stored = _ref_pairs(r)
    shas = [assets.sha_of(a) for a, _ in stored]
    h = prompts.input_hash(r["prompt"], shas, r["seed"], s["image_model"], r["width"], r["height"], "ref")
    reuse = assets.find_by_input_hash(h, "ref")
    if reuse:
        _set_ref_result(p, r, reuse["id"], h)
        return db.get("ref", r["id"])
    payload = ImageGeneratePayload(
        prompt=r["prompt"], negative_prompt=s["negative_prompt"],
        refs=[RefImage(asset_id=a, role=role, sha256=assets.sha_of(a)) for a, role in stored],
        width=r["width"], height=r["height"], seed=r["seed"], steps=s.get("image_steps"),
        meta={"purpose": "ref", "owner_type": owner_type, "label": label})
    jobs.enqueue(p["id"], "image.generate", payload.model_dump(), "ref_image", r["id"], input_hash=h,
                 model_hint=s["image_model"], priority=3)
    db.update("ref", r["id"], status="generating")
    return db.get("ref", r["id"])


def enqueue_ref(rid: int) -> None:
    r = db.get("ref", rid)
    ensure_ref(project(r["project_id"]), r["owner_type"], r["owner_id"], r["state_key"], r["label"], r["prompt"],
               _ref_pairs(r), r["width"], r["height"], r["first_chapter"])


def _set_ref_result(p: dict, r: dict, asset_id: int, h: str) -> None:
    flags = ["regenerated"] if r["regen_count"] else []
    status, reviewer = gate_status(p, None, "reference", flags, f"ref:{r['id']}:{r['regen_count']}:{h[:8]}")
    db.update("ref", r["id"], asset_id=asset_id, input_hash=h, status=status, reviewer=reviewer, flags_json=jd(flags))
    if status == "approved":
        audit(p["id"], "ref", r["id"], "approve", reviewer)


def face_ref(p: dict, char: dict, state: dict, chapter_idx: int, ensure: bool = True) -> dict | None:
    key = st.face_key(state)
    if not ensure:
        return _ref_row("character_face", char["id"], key)
    size = int(p["settings"]["ref_size"])
    return ensure_ref(p, "character_face", char["id"], key, f"Mặt: {char['name']}",
                      prompts.face_ref_prompt(p["style_prompt"], char, state), [], size, size, chapter_idx)


def outfit_ref(p: dict, char: dict, state: dict, face: dict | None, chapter_idx: int, ensure: bool = True) -> dict | None:
    key = st.outfit_key(state)
    if not ensure:
        return _ref_row("character_outfit", char["id"], key)
    if not face or face["status"] != "approved" or not face["asset_id"]:
        return None
    size = int(p["settings"]["ref_size"])
    look = st.describe(state, st.OUTFIT_FIELDS)
    return ensure_ref(p, "character_outfit", char["id"], key,
                      f"Toàn thân: {char['name']}" + (f" ({look})" if look else ""),
                      prompts.outfit_ref_prompt(p["style_prompt"], char, state),
                      [(face["asset_id"], f"the face of {char['name']}")], size * 3 // 4, size, chapter_idx)


def location_ref(p: dict, loc: dict, chapter_idx: int, variant: str, ensure: bool = True) -> dict | None:
    state = st.loc_state_at(loc["id"], chapter_idx)
    key = st.location_key(state, variant)
    if not ensure:
        return _ref_row("location", loc["id"], key)
    w, h = master_size({**p, "mode": "video"}, None)
    vt = prompts.variant_text(variant)
    cond = st.describe(state, st.LOC_FIELDS)
    label = f"Bối cảnh: {loc['name']}" + (f" · {vt}" if vt else "") + (f" · {cond}" if cond else "")
    return ensure_ref(p, "location", loc["id"], key, label,
                      prompts.location_ref_prompt(p["style_prompt"], loc, state, variant), [], w, h, chapter_idx)


def beat_waiting_identity(beat: dict) -> bool:
    for c in jl(beat["cast_json"], []):
        ch = db.get("character", c["character_id"])
        if ch and ch["status"] == "pending":
            return True
    if beat["location_id"]:
        l = db.get("location", beat["location_id"])
        if l and l["status"] == "pending":
            return True
    return False


def scene_spec(p: dict, ch: dict, beat: dict, ensure: bool = True) -> dict | None:
    """Prompt, ảnh tham chiếu và hash của ảnh gốc nhịp. None nếu tham chiếu chưa sẵn sàng."""
    s = p["settings"]
    if beat_waiting_identity(beat):
        return None
    cast = []
    for c in jl(beat["cast_json"], []):
        char = db.get("character", c["character_id"])
        if char and char["status"] == "approved":
            cast.append({**c, "char": char})
    refs: list[tuple[int, str]] = []
    cast_desc = []
    ready = True
    for i, c in enumerate(cast):
        char = c["char"]
        state = st.state_at(char["id"], ch["idx"])
        cast_desc.append({"name": char["name"], "appearance": char["appearance"], "pose": c.get("pose", ""),
                          "expression": c.get("expression", ""), "state_text": st.describe(state)})
        if i >= int(s["max_cast_refs"]):
            continue
        f = face_ref(p, char, state, ch["idx"], ensure)
        o = outfit_ref(p, char, state, f, ch["idx"], ensure)
        if not f or f["status"] != "approved":
            ready = False
            continue
        if o and o["status"] == "approved":
            refs.append((o["asset_id"], f"{char['name']}'s full body and current outfit"))
            if len(cast) == 1:
                refs.append((f["asset_id"], f"the face of {char['name']}"))
        else:
            ready = False
    loc = db.get("location", beat["location_id"]) if beat["location_id"] else None
    if loc and loc["status"] != "approved":
        loc = None
    loc_state = st.loc_state_at(loc["id"], ch["idx"]) if loc else {}
    if loc:
        lr = location_ref(p, loc, ch["idx"], beat["variant"], ensure)
        if lr and lr["status"] == "approved":
            refs.append((lr["asset_id"], f"the background location {loc['name']}"))
        else:
            ready = False
    if not ready:
        return None
    refs = refs[: int(s["max_refs"])]
    computed = prompts.scene_prompt(p["style_prompt"], beat, cast_desc, loc, loc_state, [r for _, r in refs])
    prompt = beat["prompt_override"].strip() or computed
    shas = [assets.sha_of(a) for a, _ in refs]
    h = prompts.input_hash(prompt, shas, beat["seed"], s["image_model"], beat["img_width"], beat["img_height"], "scene")
    return {"prompt": prompt, "computed": computed, "refs": refs, "hash": h}


def _enqueue_beat_image(p: dict, ch: dict, beat: dict) -> None:
    spec = scene_spec(p, ch, beat)
    if spec is None:
        return
    reuse = assets.find_by_input_hash(spec["hash"], "scene")
    if reuse:
        _set_beat_image(p, ch, beat, reuse["id"], spec)
        return
    s = p["settings"]
    payload = ImageGeneratePayload(
        prompt=spec["prompt"], negative_prompt=s["negative_prompt"],
        refs=[RefImage(asset_id=a, role=r, sha256=assets.sha_of(a)) for a, r in spec["refs"]],
        width=beat["img_width"], height=beat["img_height"], seed=beat["seed"], steps=s.get("image_steps"),
        meta={"purpose": "scene", "chapter": ch["idx"], "beat": beat["idx"],
              "spec": {"prompt": spec["prompt"], "refs": [a for a, _ in spec["refs"]]}})
    jobs.enqueue(p["id"], "image.generate", payload.model_dump(), "beat_image", beat["id"],
                 input_hash=spec["hash"], model_hint=s["image_model"], priority=1)
    db.update("beat", beat["id"], image_status="generating")


def _set_beat_image(p: dict, ch: dict, beat: dict, asset_id: int, spec: dict) -> None:
    flags = ["regenerated"] if beat["image_regen"] else []
    status, reviewer = gate_status(p, ch, "scene", flags, f"beat:{beat['id']}:{beat['image_regen']}:{spec['hash'][:8]}")
    db.update("beat", beat["id"], image_asset_id=asset_id, image_hash=spec["hash"], image_prompt=spec["prompt"],
              image_refs_json=jd([r[0] if isinstance(r, (list, tuple)) else r for r in spec.get("refs", [])]),
              image_status=status, image_flags=jd(flags), image_reviewer=reviewer)
    if status == "approved":
        audit(p["id"], "beat_image", beat["id"], "approve", reviewer)


def _enqueue_tts(p: dict, ch: dict, seg: dict) -> None:
    s = p["settings"]
    h = prompts.text_hash(seg["narration"], s["tts_voice"], s["tts_rate"], s["tts_model"], seg["audio_regen"])
    reuse = assets.find_by_input_hash(h, "audio")
    if reuse:
        meta = jl(reuse["meta_json"], {})
        _set_audio_result(p, ch, seg, reuse["id"], h, float(meta.get("duration", 0)),
                          {"sentences": meta.get("sentences", []), "words": meta.get("words", [])})
        return
    payload = TtsPayload(text=seg["narration"], voice=s["tts_voice"], rate=float(s["tts_rate"]),
                         language=s["language"], meta={"chapter": ch["idx"], "segment": seg["idx"]})
    jobs.enqueue(p["id"], "tts.synthesize", payload.model_dump(), "segment_audio", seg["id"], input_hash=h,
                 model_hint=s["tts_model"], priority=2)
    db.update("segment", seg["id"], audio_status="generating")


def _set_audio_result(p: dict, ch: dict, seg: dict, asset_id: int, h: str, duration: float, timings: dict) -> None:
    flags = checks.audio_flags(seg["narration"], duration)
    if seg["audio_regen"]:
        flags.append("regenerated")
    status, reviewer = gate_status(p, ch, "audio", flags, f"audio:{seg['id']}:{seg['audio_regen']}")
    db.update("segment", seg["id"], audio_asset_id=asset_id, audio_hash=h, audio_status=status,
              audio_flags=jd(flags), duration=duration, timings_json=jd(timings), reviewer=reviewer)
    if status == "approved":
        audit(p["id"], "segment_audio", seg["id"], "approve", reviewer)


# ================================================================ sản xuất
def produce(p: dict, ch: dict) -> None:
    for b in db.q("SELECT * FROM beat WHERE chapter_id=? AND status='approved' ORDER BY idx", ch["id"]):
        if b["image_status"] == "missing":
            _enqueue_beat_image(p, ch, b)
    if wants_video(p):
        for seg in db.q("SELECT * FROM segment WHERE chapter_id=? ORDER BY idx", ch["id"]):
            if seg["audio_status"] == "missing":
                _enqueue_tts(p, ch, seg)
    check_done(p, ch)


def chapter_progress(p: dict, ch: dict) -> dict:
    r = db.one("SELECT COUNT(*) n, SUM(image_status='approved') ok, SUM(image_status='pending') pend "
               "FROM beat WHERE chapter_id=? AND status='approved'", ch["id"])
    out = {"images": r["n"] or 0, "images_ok": r["ok"] or 0, "images_pending": r["pend"] or 0,
           "audio": 0, "audio_ok": 0, "panels": 0, "overflow": 0}
    if wants_video(p):
        a = db.one("SELECT COUNT(*) n, SUM(audio_status='approved') ok FROM segment WHERE chapter_id=?", ch["id"])
        out.update(audio=a["n"] or 0, audio_ok=a["ok"] or 0)
    if wants_comic(p):
        out["panels"] = db.val("SELECT COUNT(*) FROM panel WHERE chapter_id=?", ch["id"]) or 0
        out["overflow"] = db.val("SELECT COUNT(*) FROM panel WHERE chapter_id=? AND flags_json LIKE '%text_overflow%'",
                                 ch["id"]) or 0
    total = out["images"] + out["audio"]
    out["pct"] = int(100 * (out["images_ok"] + out["audio_ok"]) / total) if total else 0
    return out


def check_done(p: dict, ch: dict) -> None:
    pr = chapter_progress(p, ch)
    ok = pr["images"] > 0 and pr["images_ok"] == pr["images"]
    if wants_video(p):
        ok &= pr["audio_ok"] == pr["audio"]
    if not ok:
        return
    db.update("chapter", ch["id"], status="done")
    status, reviewer = gate_status(p, ch, "publish", [], f"publish:{ch['id']}")
    if status == "approved":
        audit(p["id"], "chapter", ch["id"], "publish", reviewer)
        publish(p, ch)


def publish(p: dict, ch: dict) -> None:
    if wants_video(p):
        jobs.enqueue(p["id"], "local.render_video", {"chapter_id": ch["id"]}, "render_video", ch["id"], max_attempts=1)
    if wants_comic(p):
        jobs.enqueue(p["id"], "local.compose_comic", {"chapter_id": ch["id"]}, "compose_comic", ch["id"], max_attempts=1)


# ================================================================ điều phối
def request_run(chapter_id: int) -> None:
    db.update("chapter", chapter_id, run_requested=1)
    advance_chapter(chapter_id)


def advance_project(pid: int) -> None:
    for ch in db.q("SELECT id FROM chapter WHERE project_id=? ORDER BY idx", pid):
        advance_chapter(ch["id"])


def advance_chapter(cid: int) -> None:
    ch = db.get("chapter", cid)
    if not ch:
        return
    p = project(ch["project_id"])
    if p["paused"] or ch["status"] == "error":
        return
    s = ch["status"]
    if s == "new":
        if not (p["autorun"] or ch["run_requested"]):
            return
        prev = db.one("SELECT status FROM chapter WHERE project_id=? AND idx<? ORDER BY idx DESC LIMIT 1", p["id"], ch["idx"])
        if prev and rank(prev["status"]) < rank("state_ready"):
            return
        enqueue_extract(p, ch)
        return
    if s == "review_state":
        if pending_state(ch):
            return
        db.update("chapter", ch["id"], status="state_ready")
        _after_state_ready(p, ch)
        p = project(p["id"])
        if p["paused"]:
            return
        s = "state_ready"
    if s == "state_ready":
        enqueue_beats(p, db.get("chapter", ch["id"]))
        return
    if s == "review_beats":
        if pending_beats(ch):
            return
        materialize(p, ch)
        db.update("chapter", ch["id"], status="producing")
        s = "producing"
    if s == "producing":
        produce(p, db.get("chapter", ch["id"]))


def _after_state_ready(p: dict, ch: dict) -> None:
    every = int(policy.project_policy(p).get("checkpoint_every") or 0)
    if p["autorun"] and every > 0 and ch["idx"] % every == 0:
        db.update("project", p["id"], paused=1, pause_reason=f"Điểm kiểm tra sau chương {ch['idx']}")
        audit(p["id"], "project", p["id"], "checkpoint", "system", f"chương {ch['idx']}")
        return
    nxt = db.one("SELECT id FROM chapter WHERE project_id=? AND idx>? ORDER BY idx LIMIT 1", p["id"], ch["idx"])
    if nxt:
        advance_chapter(nxt["id"])


def mark_recheck_after(pid: int, chapter_idx: int) -> None:
    db.ex("UPDATE chapter SET needs_recheck=1 WHERE project_id=? AND idx>? AND status NOT IN ('new','extracting')",
          pid, chapter_idx)


def advance_producing(pid: int) -> None:
    for ch in db.q("SELECT id FROM chapter WHERE project_id=? AND status IN ('producing','review_beats') ORDER BY idx", pid):
        advance_chapter(ch["id"])


def add_manual_event(ch: dict, subject_type: str, subject_id: int, field: str, value: str, permanent: bool = False,
                     until_chapter: int | None = None, evidence: str = "", note: str = "create") -> int:
    eid = db.insert("state_event", project_id=ch["project_id"], subject_type=subject_type, subject_id=subject_id,
                    chapter_id=ch["id"], chapter_idx=ch["idx"],
                    field=st.normalize_field(field, permanent, subject_type), value=value.strip(),
                    permanent=int(permanent or field == "mark"), until_chapter=until_chapter, evidence=evidence,
                    status="approved", reviewer="user", flags_json="[]", created_at=now())
    audit(ch["project_id"], "event", eid, note, "user")
    mark_recheck_after(ch["project_id"], ch["idx"])
    refresh_refs(ch["project_id"], ch["idx"])
    return eid


def remove_mark(character_id: int, mark: str, chapter_id: int) -> int:
    """Xóa đúng một dấu vết từ chương chỉ định, giữ các dấu vết khác."""
    ch = db.get("chapter", chapter_id)
    return add_manual_event(ch, "character", character_id, "mark", "-" + mark.strip(), True,
                            evidence="(xóa thủ công)", note="remove_mark")


# ================================================================ kết quả job
def _artifact(job: dict, name: str) -> Path:
    path = jobs.artifact_dir(job["id"]) / Path(name).name
    if not path.exists():
        raise FileNotFoundError(f"Thiếu file kết quả {name} của job {job['id']}")
    return path


def handle_ref_image(job: dict, output: dict) -> None:
    r = db.get("ref", job["owner_id"])
    if not r:
        return
    p = project(r["project_id"])
    aid = assets.save_file(_artifact(job, output["files"][0]), "ref", p["id"], input_hash=job["input_hash"],
                           model_id=job["model_id"], meta={"label": r["label"]})
    _set_ref_result(p, r, aid, job["input_hash"])
    advance_producing(p["id"])


def handle_beat_image(job: dict, output: dict) -> None:
    b = db.get("beat", job["owner_id"])
    if not b:
        return
    ch = db.get("chapter", b["chapter_id"])
    p = project(ch["project_id"])
    spec = jl(job["payload_json"], {}).get("meta", {}).get("spec", {})
    aid = assets.save_file(_artifact(job, output["files"][0]), "scene", p["id"], input_hash=job["input_hash"],
                           model_id=job["model_id"], meta={"beat": b["id"]})
    _set_beat_image(p, ch, b, aid, {"hash": job["input_hash"], "prompt": spec.get("prompt", ""), "refs": spec.get("refs", [])})
    advance_chapter(ch["id"])


def handle_tts(job: dict, output: dict) -> None:
    seg = db.get("segment", job["owner_id"])
    if not seg:
        return
    ch = db.get("chapter", seg["chapter_id"])
    p = project(ch["project_id"])
    dur = float(output.get("duration") or 0)
    timings = {"sentences": output.get("sentences") or [], "words": output.get("words") or []}
    aid = assets.save_file(_artifact(job, output["file"]), "audio", p["id"], input_hash=job["input_hash"],
                           model_id=job["model_id"], meta={"duration": dur, **timings})
    _set_audio_result(p, ch, seg, aid, job["input_hash"], dur, timings)
    advance_chapter(ch["id"])


def handle_render_video(job: dict, output: dict) -> None:
    db.update("chapter", job["owner_id"], video_asset_id=output.get("video_asset_id"),
              srt_asset_id=output.get("srt_asset_id"), error="")


def handle_compose_comic(job: dict, output: dict) -> None:
    db.update("chapter", job["owner_id"], comic_cbz_asset_id=output.get("cbz_asset_id"),
              comic_pdf_asset_id=output.get("pdf_asset_id"))


def handle_dedupe(job: dict, output: dict) -> None:
    from . import dedupe

    text = output.get("text", "")
    try:
        data = llm.DedupeOut.model_validate(llm.extract_json(text))
    except (ValueError, ValidationError) as e:
        jobs.retry_with_feedback(job, text, str(e))
        return
    dedupe.apply_llm(job["owner_id"], data)


HANDLERS = {
    "chapter_extract": handle_extract, "chapter_beats": handle_beats, "ref_image": handle_ref_image,
    "beat_image": handle_beat_image, "segment_audio": handle_tts, "render_video": handle_render_video,
    "compose_comic": handle_compose_comic, "project_dedupe": handle_dedupe,
}


def handle_job_done(job: dict, output: dict) -> None:
    h = HANDLERS.get(job["owner_type"])
    if h is None:
        log.warning("Không có handler cho %s", job["owner_type"])
        return
    h(job, output)


def handle_job_failed(job: dict) -> None:
    ot, oid = job["owner_type"], job["owner_id"]
    err = (job.get("error") or "")[:2000]
    if ot in ("chapter_extract", "chapter_beats"):
        db.update("chapter", oid, status="error", error=err or "Job đã bị hủy")
    elif ot in ("render_video", "compose_comic"):
        db.update("chapter", oid, error=f"Xuất bản lỗi: {err}")
    elif ot == "ref_image":
        db.update("ref", oid, status="failed")
    elif ot == "beat_image":
        db.update("beat", oid, image_status="failed")
    elif ot == "segment_audio":
        db.update("segment", oid, audio_status="failed")


# ================================================================ tạo lại, ảnh cũ
def _reopen_chapter(chapter_id: int) -> None:
    db.ex("UPDATE chapter SET status='producing' WHERE id=? AND status='done'", chapter_id)


def regen_ref(rid: int, new_seed: bool = True) -> None:
    r = db.get("ref", rid)
    if not r:
        return
    db.update("ref", rid, status="missing", seed=r["seed"] + (1 if new_seed else 0), regen_count=r["regen_count"] + 1)
    enqueue_ref(rid)
    advance_producing(r["project_id"])


def regen_beat_image(beat_id: int, new_seed: bool = True) -> None:
    b = db.get("beat", beat_id)
    if not b:
        return
    db.update("beat", beat_id, image_status="missing", seed=b["seed"] + (1 if new_seed else 0),
              image_regen=b["image_regen"] + 1)
    _reopen_chapter(b["chapter_id"])
    advance_chapter(b["chapter_id"])


def regen_audio(seg_id: int) -> None:
    seg = db.get("segment", seg_id)
    if not seg:
        return
    db.update("segment", seg_id, audio_status="missing", audio_regen=seg["audio_regen"] + 1)
    _reopen_chapter(seg["chapter_id"])
    advance_chapter(seg["chapter_id"])


def refresh_refs(pid: int, from_chapter_idx: int = 0) -> None:
    """Sau khi trạng thái / mô tả đổi: tạo trước ảnh tham chiếu mới cho các chương đã sản xuất để đánh dấu đúng
    "ảnh cũ". Không tự tạo lại ảnh cảnh (người dùng quyết định)."""
    p = project(pid)
    for ch in db.q("SELECT * FROM chapter WHERE project_id=? AND idx>=? AND status IN ('producing','done') ORDER BY idx",
                   pid, from_chapter_idx):
        for b in db.q("SELECT * FROM beat WHERE chapter_id=? AND status='approved'", ch["id"]):
            scene_spec(p, ch, b, ensure=True)
    advance_producing(pid)


def stale_info(p: dict, ch: dict, beat: dict) -> dict:
    if not beat["image_asset_id"] or beat["image_locked"]:
        return {"stale": False}
    spec = scene_spec(p, ch, beat, ensure=False)
    if spec is None:
        return {"stale": True, "waiting": True, "diff": "", "refs_changed": True, "prompt_changed": False}
    if spec["hash"] == beat["image_hash"]:
        return {"stale": False}
    old_refs = set(jl(beat["image_refs_json"], []))
    return {"stale": True, "diff": prompts.word_diff(beat["image_prompt"], spec["prompt"]),
            "prompt_changed": beat["image_prompt"] != spec["prompt"],
            "refs_changed": old_refs != {a for a, _ in spec["refs"]}, "new_prompt": spec["prompt"]}


def regen_stale(chapter_id: int) -> int:
    ch = db.get("chapter", chapter_id)
    p = project(ch["project_id"])
    n = 0
    for b in db.q("SELECT * FROM beat WHERE chapter_id=? AND status='approved'", chapter_id):
        if stale_info(p, ch, b)["stale"]:
            db.update("beat", b["id"], image_status="missing")
            n += 1
    if n:
        _reopen_chapter(chapter_id)
    advance_chapter(chapter_id)
    return n


def reset_chapter(chapter_id: int, to: str) -> None:
    ch = db.get("chapter", chapter_id)
    for j in db.q("SELECT id FROM job WHERE owner_type IN ('chapter_extract','chapter_beats') AND owner_id=? "
                  "AND status='queued'", ch["id"]):
        db.update("job", j["id"], status="cancelled", finished_at=now())
    with db.tx():
        db.ex("DELETE FROM beat WHERE chapter_id=?", chapter_id)
        db.ex("DELETE FROM page WHERE chapter_id=?", chapter_id)
        if to == "extract":
            db.update("chapter", chapter_id, status="new", run_requested=1, error="", needs_recheck=0)
        else:
            db.update("chapter", chapter_id, status="state_ready", error="")
    advance_chapter(chapter_id)


# ================================================================ việc cục bộ
def _local_progress(job_id: int):
    def report(frac: float, msg: str = "") -> None:
        jobs.record_progress(job_id, frac, msg)
    return report


def settings_warnings(p: dict) -> list[dict]:
    return validate.warnings(p, p["settings"])


def run_local_job(job: dict) -> dict:
    cfg = get_settings()
    payload = jl(job["payload_json"], {})
    ch = db.get("chapter", payload["chapter_id"])
    p = project(ch["project_id"])
    s = p["settings"]
    work = jobs.artifact_dir(job["id"])
    report = _local_progress(job["id"])
    if job["kind"] == "local.render_video":
        system.require_ffmpeg()
        segs = db.q("""SELECT s.*, b.image_asset_id, b.shot FROM segment s JOIN beat b ON b.id=s.beat_id
                       WHERE s.chapter_id=? ORDER BY s.idx""", ch["id"])
        items = [{"image": assets.path_of(sg["image_asset_id"]), "audio": assets.path_of(sg["audio_asset_id"]),
                  "duration": sg["duration"] or video.estimate_duration(sg["narration"]),
                  "focus_x": sg["focus_x"], "focus_y": sg["focus_y"], "motion": sg["motion"], "shot": sg["shot"]}
                 for sg in segs]
        out, durs = video.render(items, work, cfg.ffmpeg, cfg.ffprobe, int(s["video_width"]), int(s["video_height"]),
                                 int(s["video_fps"]), s["video_encoder"], float(s["video_zoom"]),
                                 float(s["video_transition"]), float(s["video_gap"]), bool(s["video_loudnorm"]), report)
        cues, t = [], 0.0
        for sg, d in zip(segs, durs):
            cues += video.cues_for_segment(sg["narration"], sg["duration"] or d, jl(sg["timings_json"], {}), t)
            t += d
        vid = assets.save_file(out, "video", p["id"], meta={"chapter": ch["idx"]})
        srt_path = work / "chapter.srt"
        srt_path.write_text(video.build_srt(cues), encoding="utf-8")
        return {"video_asset_id": vid, "srt_asset_id": assets.save_file(srt_path, "subtitle", p["id"])}
    if job["kind"] == "local.compose_comic":
        font = comic.find_font(cfg.font_path)
        g = comic_geometry(p)
        files = []
        pages = db.q("SELECT * FROM page WHERE chapter_id=? ORDER BY idx", ch["id"])
        for i, pg in enumerate(pages):
            panels = db.q("""SELECT pn.*, b.image_asset_id FROM panel pn JOIN beat b ON b.id=pn.beat_id
                             WHERE pn.page_id=? ORDER BY pn.slot""", pg["id"])
            imgs = {pn["id"]: assets.path_of(pn["image_asset_id"]) for pn in panels}
            balloons = {pn["id"]: db.q("SELECT * FROM balloon WHERE panel_id=? ORDER BY idx", pn["id"]) for pn in panels}
            img, over = comic.compose_page(pg["width"], pg["height"], panels, imgs, balloons, font, g["margin"],
                                           g["gutter"], base_font(pg["width"]))
            for pn in panels:
                flags = set(jl(pn["flags_json"], []))
                flags = flags | {"text_overflow"} if pn["id"] in over else flags - {"text_overflow"}
                db.update("panel", pn["id"], flags_json=jd(sorted(flags)))
            f = work / f"page_{pg['idx'] + 1:03d}.png"
            img.save(f, "PNG")
            aid = assets.save_file(f, "page", p["id"], meta={"chapter": ch["idx"], "page": pg["idx"]})
            db.update("page", pg["id"], asset_id=aid)
            files.append(assets.path_of(aid))
            report((i + 1) / (len(pages) + 1), f"trang {i + 1}/{len(pages)}")
        cbz = comic.export_cbz(files, work / "chapter.cbz")
        pdf = comic.export_pdf(files, work / "chapter.pdf")
        return {"cbz_asset_id": assets.save_file(cbz, "comic", p["id"]),
                "pdf_asset_id": assets.save_file(pdf, "comic", p["id"])}
    raise ValueError(f"Không biết local job {job['kind']}")


def local_tick(worker_id: str = "local-runner") -> bool:
    from storyforge.protocol import LOCAL_KINDS

    job = jobs.claim(worker_id, list(LOCAL_KINDS), [])
    if not job:
        return False
    t0 = now()
    try:
        jobs.complete(job["id"], run_local_job(job), "local", now() - t0)
    except system.ToolMissing as e:
        jobs.fail(job["id"], str(e), retryable=False)
    except FileNotFoundError as e:
        jobs.fail(job["id"], f"Không tìm thấy chương trình hoặc tệp: {e}. {system.INSTALL_HINT}", retryable=False)
    except Exception as e:  # noqa: BLE001
        log.exception("Local job %s lỗi", job["id"])
        jobs.fail(job["id"], str(e), retryable=False)
    finally:
        shutil.rmtree(jobs.artifact_dir(job["id"]), ignore_errors=True)
    return True
