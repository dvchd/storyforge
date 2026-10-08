"""Engine dieu phoi quy trinh.

Luong chung:  chuong -> trich trang thai -> duyet -> chia nhip -> duyet -> san xuat
San xuat:     anh tham chieu -> anh canh (video) / anh khung (truyen tranh) -> giong doc
Xuat ban:     dung video (ffmpeg) / ghep trang truyen tranh (Pillow)

Engine chi tao job va xu ly ket qua. Moi muc deu qua cong duyet (policy.decide).
Buoc tiep theo chi duoc tao sau khi muc truoc da duoc duyet, du la duyet tay hay tu dong.
"""
from __future__ import annotations

import logging
import random
import shutil
from pathlib import Path

from pydantic import ValidationError

from storyforge.protocol import ImageGeneratePayload, LlmChatPayload, RefImage, TtsPayload

from . import assets, checks, comic, jobs, llm, policy, prompts, state as st, video
from .config import get_settings
from .db import db, jd, jl, now

log = logging.getLogger("storyforge.engine")

DEFAULT_SETTINGS: dict = {
    "llm_model": "", "llm_extra": "", "llm_temperature": 0.2, "llm_max_tokens": None,
    "image_model": "", "image_steps": None, "negative_prompt": "", "ref_size": 1024,
    "tts_model": "", "tts_voice": "", "tts_rate": 1.0, "language": "vi",
    "video_width": 1344, "video_height": 768, "video_fps": 30, "video_encoder": "auto", "video_zoom": 0.10,
    "comic_page_width": 1600, "comic_page_height": 2400, "comic_panels_per_page": 4, "comic_panel_area": 1048576,
    "context_chapters": 3, "max_cast_refs": 2, "max_refs": 4,
    "auto_regen_on_reject": True, "max_regen": 3,
}

STATUS_ORDER = ["new", "extracting", "review_state", "state_ready", "beating", "review_beats", "producing", "done"]
STATUS_LABELS = {
    "new": "Mới", "extracting": "Đang trích trạng thái", "review_state": "Chờ duyệt trạng thái",
    "state_ready": "Trạng thái xong", "beating": "Đang chia nhịp", "review_beats": "Chờ duyệt nhịp",
    "producing": "Đang sản xuất", "done": "Hoàn tất", "error": "Lỗi",
}


def rank(s: str) -> int:
    return STATUS_ORDER.index(s) if s in STATUS_ORDER else -1


# ================================================================ basics
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
    rng = random.Random(f"{p['id']}:{gate}:{key}")
    status = policy.decide(gp, set(flags), rng)
    reviewer = policy.policy_name(p, ch, gate) if status == "approved" else ""
    return status, reviewer


def gate_batch(p: dict, ch: dict | None, gate: str) -> str:
    return policy.effective(p, ch)[gate].batch


def chapter_of_idx(pid: int, idx: int) -> dict | None:
    return db.one("SELECT * FROM chapter WHERE project_id=? AND idx=?", pid, idx)


# ================================================================ characters
def all_chars(pid: int, include_rejected: bool = False) -> list[dict]:
    sql = "SELECT * FROM character WHERE project_id=?" + ("" if include_rejected else " AND status!='rejected'")
    rows = db.q(sql + " ORDER BY first_chapter, id", pid)
    for r in rows:
        r["aliases"] = jl(r["aliases_json"], [])
    return rows


def name_index(pid: int) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for c in all_chars(pid):
        for n in [c["name"], *c["aliases"]]:
            if n:
                idx.setdefault(checks.norm(n), c)
    return idx


def chars_context(pid: int, chapter_idx: int) -> list[dict]:
    out = []
    for c in all_chars(pid):
        if c["first_chapter"] > chapter_idx:
            continue
        out.append({**c, "state": st.state_at(c["id"], chapter_idx)})
    return out


def locs(pid: int, include_rejected: bool = False) -> list[dict]:
    sql = "SELECT * FROM location WHERE project_id=?" + ("" if include_rejected else " AND status!='rejected'")
    return db.q(sql + " ORDER BY first_chapter, id", pid)


def find_location(pid: int, name: str, variant: str) -> dict | None:
    n, v = checks.norm(name), checks.norm(variant or "default")
    best = None
    for l in locs(pid):
        if checks.norm(l["name"]) == n:
            if checks.norm(l["variant"]) == v:
                return l
            if checks.norm(l["variant"]) == "default":
                best = l
    return best


def create_character(p: dict, ch: dict, name: str, aliases: list[str], appearance: str, role: str,
                     extra_flags: list[str] | None = None) -> dict:
    flags = ["new_identity", *(extra_flags or [])]
    status, reviewer = gate_status(p, ch, "identity", flags, f"char:{name}")
    cid = db.insert("character", project_id=p["id"], name=name.strip(), aliases_json=jd(aliases or []),
                    appearance=appearance or "", role=role or "", first_chapter=ch["idx"], status=status,
                    flags_json=jd(flags), reviewer=reviewer, created_at=now())
    if status == "approved":
        audit(p["id"], "character", cid, "approve", reviewer)
    c = db.get("character", cid)
    c["aliases"] = aliases or []
    return c


def create_location(p: dict, ch: dict, name: str, variant: str, description: str,
                    extra_flags: list[str] | None = None) -> dict:
    flags = ["new_identity", *(extra_flags or [])]
    status, reviewer = gate_status(p, ch, "identity", flags, f"loc:{name}:{variant}")
    lid = db.insert("location", project_id=p["id"], name=name.strip(), variant=(variant or "default").strip(),
                    description=description or "", first_chapter=ch["idx"], status=status,
                    flags_json=jd(flags), reviewer=reviewer, created_at=now())
    if status == "approved":
        audit(p["id"], "location", lid, "approve", reviewer)
    return db.get("location", lid)


# ================================================================ extraction
def enqueue_extract(p: dict, ch: dict) -> None:
    s = p["settings"]
    prev = db.q("SELECT idx, summary FROM chapter WHERE project_id=? AND idx<? ORDER BY idx DESC LIMIT ?",
                p["id"], ch["idx"], int(s["context_chapters"]))[::-1]
    chars = chars_context(p["id"], ch["idx"] - 1)
    lc = locs(p["id"])
    msgs = llm.extract_messages(p, ch, chars, lc, prev, s.get("llm_extra", ""))
    payload = LlmChatPayload(
        messages=msgs, json_schema=llm.schema_of(llm.ChapterExtraction), schema_name="ChapterExtraction",
        temperature=float(s["llm_temperature"]), max_tokens=s.get("llm_max_tokens"),
        meta={"task": "extract", "chapter_text": ch["text"],
              "known_characters": [{"name": c["name"], "aliases": c["aliases"]} for c in chars],
              "known_locations": [l["name"] for l in lc]},
    )
    jobs.enqueue(p["id"], "llm.chat", payload.model_dump(), "chapter_extract", ch["id"],
                 model_hint=s["llm_model"], priority=5)
    db.update("chapter", ch["id"], status="extracting", error="")


def handle_extract(job: dict, output: dict) -> None:
    ch = db.get("chapter", job["owner_id"])
    if not ch:
        return
    p = project(ch["project_id"])
    text = output.get("text", "")
    try:
        data = llm.ChapterExtraction.model_validate(llm.extract_json(text))
    except (ValueError, ValidationError) as e:
        if not jobs.retry_with_feedback(job, text, str(e)):
            db.update("chapter", ch["id"], status="error", error=f"LLM trả sai schema: {e}"[:2000])
        return
    with db.tx():
        db.ex("DELETE FROM state_event WHERE chapter_id=? AND status!='approved'", ch["id"])
        idx = name_index(p["id"])
        for nc in data.new_characters:
            key = checks.norm(nc.name)
            if not key:
                continue
            if key in idx:
                c = idx[key]
                merged = list(dict.fromkeys([*c["aliases"], *nc.aliases]))
                if merged != c["aliases"]:
                    db.update("character", c["id"], aliases_json=jd(merged))
                if not c["appearance"] and nc.appearance:
                    db.update("character", c["id"], appearance=nc.appearance)
                continue
            c = create_character(p, ch, nc.name, nc.aliases, nc.appearance, nc.role)
            for n in [c["name"], *c["aliases"]]:
                idx[checks.norm(n)] = c
        for nl in data.locations:
            if nl.name.strip() and not find_location(p["id"], nl.name, nl.variant):
                create_location(p, ch, nl.name, nl.variant, nl.description)
        state_gp_batch = gate_batch(p, ch, "state")
        created = []
        for ev in data.events:
            key = checks.norm(ev.character)
            c = idx.get(key)
            if c is None:
                c = create_character(p, ch, ev.character, [], "", "", ["unknown_character"])
                idx[key] = c
            current = st.state_at(c["id"], ch["idx"] - 1)
            flags = checks.event_flags(ev.field, ev.value, ev.permanent, ev.evidence, ch["text"], current)
            if "unknown_character" in (jl(c.get("flags_json"), []) or []) and c["status"] != "approved":
                flags.append("unknown_character")
            until = ch["idx"] + ev.lasts_chapters - 1 if ev.lasts_chapters and not ev.permanent else None
            status, reviewer = gate_status(p, ch, "state", flags, f"ev:{ch['id']}:{c['id']}:{ev.field}:{ev.value}")
            eid = db.insert("state_event", project_id=p["id"], character_id=c["id"], chapter_id=ch["id"],
                            chapter_idx=ch["idx"], field=ev.field, value=ev.value.strip(),
                            permanent=int(ev.permanent), until_chapter=until, evidence=ev.evidence,
                            status=status, flags_json=jd(flags), reviewer=reviewer, created_at=now())
            created.append((eid, status, reviewer))
        if state_gp_batch == "chapter" and any(s != "approved" for _, s, _ in created):
            for eid, _, _ in created:
                db.update("state_event", eid, status="pending", reviewer="")
        else:
            for eid, s, r in created:
                if s == "approved":
                    audit(p["id"], "event", eid, "approve", r)
        db.update("chapter", ch["id"], summary=data.summary.strip(), status="review_state", needs_recheck=0)
    advance_chapter(ch["id"])


def pending_identity(ch: dict) -> int:
    return (db.val("SELECT COUNT(*) FROM character WHERE project_id=? AND first_chapter=? AND status='pending'",
                   ch["project_id"], ch["idx"]) or 0) + \
           (db.val("SELECT COUNT(*) FROM location WHERE project_id=? AND first_chapter=? AND status='pending'",
                   ch["project_id"], ch["idx"]) or 0)


def pending_state(ch: dict) -> int:
    return pending_identity(ch) + (db.val(
        "SELECT COUNT(*) FROM state_event WHERE chapter_id=? AND status='pending'", ch["id"]) or 0)


# ================================================================ beats
def enqueue_beats(p: dict, ch: dict) -> None:
    s = p["settings"]
    paras = llm.paragraphs(ch["text"])
    chars = chars_context(p["id"], ch["idx"])
    lc = locs(p["id"])
    msgs = llm.beats_messages(p, ch, paras, chars, lc, s.get("llm_extra", ""))
    payload = LlmChatPayload(
        messages=msgs, json_schema=llm.schema_of(llm.BeatsOut), schema_name="BeatsOut",
        temperature=float(s["llm_temperature"]), max_tokens=s.get("llm_max_tokens"),
        meta={"task": "beats", "paragraphs": paras,
              "known_characters": [{"name": c["name"], "aliases": c["aliases"]} for c in chars],
              "known_locations": [{"name": l["name"], "variant": l["variant"]} for l in lc]},
    )
    jobs.enqueue(p["id"], "llm.chat", payload.model_dump(), "chapter_beats", ch["id"],
                 model_hint=s["llm_model"], priority=4)
    db.update("chapter", ch["id"], status="beating", error="")


def _normalize_spans(beats: list[llm.BeatOut], n: int) -> tuple[list[tuple[int, int]], set[int]]:
    spans, adjusted = [], set()
    nxt = 1
    ordered = sorted(enumerate(beats), key=lambda t: (t[1].start, t[1].end))
    for i, (orig_i, b) in enumerate(ordered):
        s, e = b.start, b.end
        if s != nxt:
            adjusted.add(orig_i)
            s = nxt
        e = max(s, min(e, n))
        if i == len(ordered) - 1 and e != n:
            adjusted.add(orig_i)
            e = n
        spans.append((s, e))
        nxt = e + 1
        if nxt > n:
            break
    order = [o for o, _ in ordered][:len(spans)]
    return list(zip(order, spans)), adjusted


def handle_beats(job: dict, output: dict) -> None:
    ch = db.get("chapter", job["owner_id"])
    if not ch:
        return
    p = project(ch["project_id"])
    text = output.get("text", "")
    try:
        data = llm.BeatsOut.model_validate(llm.extract_json(text))
        if not data.beats:
            raise ValueError("Danh sách beats rỗng")
    except (ValueError, ValidationError) as e:
        if not jobs.retry_with_feedback(job, text, str(e)):
            db.update("chapter", ch["id"], status="error", error=f"LLM trả sai schema: {e}"[:2000])
        return
    paras = llm.paragraphs(ch["text"])
    spans, adjusted = _normalize_spans(data.beats, len(paras))
    with db.tx():
        db.ex("DELETE FROM beat WHERE chapter_id=?", ch["id"])
        db.ex("DELETE FROM page WHERE chapter_id=?", ch["id"])
        idx = name_index(p["id"])
        created = []
        for k, (orig_i, (s, e)) in enumerate(spans):
            b = data.beats[orig_i]
            flags = ["span_adjusted"] if orig_i in adjusted else []
            loc_id = None
            if b.location.strip():
                loc = find_location(p["id"], b.location, b.variant)
                if loc is None:
                    loc = create_location(p, ch, b.location, b.variant, b.action, ["unknown_location"])
                    flags.append("unknown_location")
                loc_id = loc["id"]
            else:
                flags.append("no_location")
            cast = []
            for ci in b.cast[:3]:
                key = checks.norm(ci.character)
                c = idx.get(key)
                if c is None:
                    c = create_character(p, ch, ci.character, [], "", "", ["unknown_character"])
                    idx[key] = c
                    flags.append("unknown_character")
                cast.append({"character_id": c["id"], "name": c["name"], "pose": ci.pose, "expression": ci.expression})
            if len(b.cast) > 3:
                flags.append("too_many_cast")
            dialogue = [{"character": d.character, "text": d.text, "kind": d.kind} for d in b.dialogue]
            status, reviewer = gate_status(p, ch, "breakdown", flags, f"beat:{ch['id']}:{k}")
            bid = db.insert("beat", project_id=p["id"], chapter_id=ch["id"], idx=k, para_start=s, para_end=e,
                            narration="\n\n".join(paras[s - 1:e]), location_id=loc_id, cast_json=jd(cast),
                            shot=b.shot, action=b.action, mood=b.mood, dialogue_json=jd(dialogue),
                            status=status, flags_json=jd(sorted(set(flags))), reviewer=reviewer)
            created.append((bid, status, reviewer))
        if gate_batch(p, ch, "breakdown") == "chapter" and any(s != "approved" for _, s, _ in created):
            db.ex("UPDATE beat SET status='pending', reviewer='' WHERE chapter_id=?", ch["id"])
        else:
            for bid, s, r in created:
                if s == "approved":
                    audit(p["id"], "beat", bid, "approve", r)
        db.update("chapter", ch["id"], status="review_beats")
    advance_chapter(ch["id"])


def pending_beats(ch: dict) -> int:
    return (db.val("SELECT COUNT(*) FROM beat WHERE chapter_id=? AND status='pending'", ch["id"]) or 0) + pending_identity(ch)


# ================================================================ materialize
def materialize(p: dict, ch: dict) -> None:
    s = p["settings"]
    beats = db.q("SELECT * FROM beat WHERE chapter_id=? AND status='approved' ORDER BY idx", ch["id"])
    with db.tx():
        if wants_video(p) and not db.val("SELECT COUNT(*) FROM segment WHERE chapter_id=?", ch["id"]):
            for i, b in enumerate(beats):
                db.insert("segment", project_id=p["id"], chapter_id=ch["id"], beat_id=b["id"], idx=i,
                          narration=b["narration"], seed=prompts.seed_for("seg", b["id"]))
        if wants_comic(p) and not db.val("SELECT COUNT(*) FROM page WHERE chapter_id=?", ch["id"]):
            pw, ph = int(s["comic_page_width"]), int(s["comic_page_height"])
            names = {checks.norm(c["name"]): c["id"] for c in all_chars(p["id"])}
            for pi, (layout, group) in enumerate(comic.plan_pages(beats, int(s["comic_panels_per_page"]))):
                page_id = db.insert("page", project_id=p["id"], chapter_id=ch["id"], idx=pi, layout=layout)
                rects = comic.LAYOUTS[layout]
                for slot, b in enumerate(group):
                    w, h = comic.panel_size(rects[slot], pw, ph, int(s["comic_panel_area"]))
                    pid = db.insert("panel", project_id=p["id"], chapter_id=ch["id"], page_id=page_id,
                                    beat_id=b["id"], slot=slot, width=w, height=h,
                                    seed=prompts.seed_for("panel", b["id"]))
                    for bi, bl in enumerate(comic.default_balloons(b, jl(b["dialogue_json"], []), names)):
                        db.insert("balloon", panel_id=pid, idx=bi, **bl)


# ================================================================ references
def _ref_row(owner_type: str, owner_id: int, key: str) -> dict | None:
    return db.one("SELECT * FROM ref WHERE owner_type=? AND owner_id=? AND state_key=?", owner_type, owner_id, key)


def ensure_ref(p: dict, owner_type: str, owner_id: int, key: str, label: str, prompt: str,
               refs: list[tuple[int, str]], width: int, height: int) -> dict:
    s = p["settings"]
    r = _ref_row(owner_type, owner_id, key)
    if r is None:
        rid = db.insert("ref", project_id=p["id"], owner_type=owner_type, owner_id=owner_id, state_key=key,
                        label=label, prompt=prompt, refs_json=jd([a for a, _ in refs]), width=width, height=height,
                        seed=prompts.seed_for(owner_type, owner_id, key), status="missing", created_at=now())
        r = db.get("ref", rid)
    if r["status"] != "missing":
        return r
    if r["locked"]:
        return r
    # Prompt luon lay ban moi nhat khi tao (tao lai) anh.
    if not r["locked"]:
        db.update("ref", r["id"], prompt=prompt, label=label, refs_json=jd([a for a, _ in refs]))
        r = db.get("ref", r["id"])
    shas = [assets.sha_of(a) for a, _ in refs]
    h = prompts.input_hash(r["prompt"], shas, r["seed"], s["image_model"], r["width"], r["height"], "ref")
    reuse = assets.find_by_input_hash(h, "ref")
    if reuse:
        _set_ref_result(p, r, reuse["id"], h, regenerated=False)
        return db.get("ref", r["id"])
    payload = ImageGeneratePayload(
        prompt=r["prompt"], negative_prompt=s["negative_prompt"],
        refs=[RefImage(asset_id=a, role=role, sha256=assets.sha_of(a)) for a, role in refs],
        width=r["width"], height=r["height"], seed=r["seed"], steps=s.get("image_steps"),
        meta={"purpose": "ref", "owner_type": owner_type, "label": label})
    jobs.enqueue(p["id"], "image.generate", payload.model_dump(), "ref_image", r["id"], input_hash=h,
                 model_hint=s["image_model"], priority=3)
    db.update("ref", r["id"], status="generating")
    return db.get("ref", r["id"])


def _set_ref_result(p: dict, r: dict, asset_id: int, h: str, regenerated: bool) -> None:
    flags = ["regenerated"] if r["regen_count"] else []
    status, reviewer = gate_status(p, None, "reference", flags, f"ref:{r['id']}:{r['regen_count']}")
    db.update("ref", r["id"], asset_id=asset_id, input_hash=h, status=status, reviewer=reviewer, flags_json=jd(flags))
    if status == "approved":
        audit(p["id"], "ref", r["id"], "approve", reviewer)


def face_ref(p: dict, char: dict, state: dict) -> dict:
    size = int(p["settings"]["ref_size"])
    key = st.face_key(state)
    return ensure_ref(p, "character_face", char["id"], key, f"Mặt: {char['name']}",
                      prompts.face_ref_prompt(p["style_prompt"], char, state), [], size, size)


def outfit_ref(p: dict, char: dict, state: dict, face: dict) -> dict | None:
    if face["status"] != "approved" or not face["asset_id"]:
        return None
    size = int(p["settings"]["ref_size"])
    key = st.outfit_key(state)
    look = st.describe(state, st.OUTFIT_FIELDS)
    return ensure_ref(p, "character_outfit", char["id"], key,
                      f"Toàn thân: {char['name']}" + (f" ({look})" if look else ""),
                      prompts.outfit_ref_prompt(p["style_prompt"], char, state),
                      [(face["asset_id"], f"the face of {char['name']}")], size * 3 // 4, size)


def location_ref(p: dict, loc: dict) -> dict:
    s = p["settings"]
    return ensure_ref(p, "location", loc["id"], "base", f"Bối cảnh: {loc['name']} [{loc['variant']}]",
                      prompts.location_ref_prompt(p["style_prompt"], loc), [],
                      int(s["video_width"]), int(s["video_height"]))


def beat_cast(p: dict, beat: dict) -> list[dict]:
    """Nhan vat da duyet trong beat, gioi han so anh tham chieu."""
    out = []
    for c in jl(beat["cast_json"], []):
        ch = db.get("character", c["character_id"])
        if ch and ch["status"] == "approved":
            out.append({**c, "char": ch})
    return out


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


def scene_spec(p: dict, ch: dict, beat: dict, width: int, height: int, seed: int, ensure: bool = True) -> dict | None:
    """Tinh prompt, anh tham chieu va hash. Tra None neu tham chieu chua san sang."""
    s = p["settings"]
    if beat_waiting_identity(beat):
        return None
    cast = beat_cast(p, beat)
    max_cast = int(s["max_cast_refs"])
    refs: list[tuple[int, str]] = []
    cast_desc = []
    ready = True
    for i, c in enumerate(cast):
        char = c["char"]
        state = st.state_at(char["id"], ch["idx"])
        cast_desc.append({"name": char["name"], "appearance": char["appearance"], "pose": c.get("pose", ""),
                          "expression": c.get("expression", ""), "state_text": st.describe(state)})
        if i >= max_cast:
            continue
        if ensure:
            f = face_ref(p, char, state)
            o = outfit_ref(p, char, state, f)
        else:
            f = _ref_row("character_face", char["id"], st.face_key(state))
            o = _ref_row("character_outfit", char["id"], st.outfit_key(state))
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
    if loc:
        lr = location_ref(p, loc) if ensure else _ref_row("location", loc["id"], "base")
        if lr and lr["status"] == "approved":
            refs.append((lr["asset_id"], f"the background location {loc['name']}"))
        else:
            ready = False
    if not ready:
        return None
    refs = refs[: int(s["max_refs"])]
    prompt = prompts.scene_prompt(p["style_prompt"], beat, cast_desc, loc, [r for _, r in refs])
    shas = [assets.sha_of(a) for a, _ in refs]
    h = prompts.input_hash(prompt, shas, seed, s["image_model"], width, height, "scene")
    return {"prompt": prompt, "refs": refs, "hash": h}


def _enqueue_scene(p: dict, ch: dict, table: str, row: dict, beat: dict, w: int, h: int) -> None:
    spec = scene_spec(p, ch, beat, w, h, row["seed"])
    if spec is None:
        return
    reuse = assets.find_by_input_hash(spec["hash"], "scene")
    if reuse:
        _set_scene_result(p, ch, table, row, reuse["id"], spec["hash"])
        return
    s = p["settings"]
    payload = ImageGeneratePayload(
        prompt=spec["prompt"], negative_prompt=s["negative_prompt"],
        refs=[RefImage(asset_id=a, role=r, sha256=assets.sha_of(a)) for a, r in spec["refs"]],
        width=w, height=h, seed=row["seed"], steps=s.get("image_steps"),
        meta={"purpose": table, "chapter": ch["idx"], "beat": beat["idx"]})
    jobs.enqueue(p["id"], "image.generate", payload.model_dump(), f"{table}_image", row["id"],
                 input_hash=spec["hash"], model_hint=s["image_model"], priority=1)
    db.update(table, row["id"], image_status="generating")


def _set_scene_result(p: dict, ch: dict, table: str, row: dict, asset_id: int, h: str) -> None:
    flags = ["regenerated"] if row["image_regen"] else []
    status, reviewer = gate_status(p, ch, "scene", flags, f"{table}:{row['id']}:{row['image_regen']}")
    db.update(table, row["id"], image_asset_id=asset_id, image_hash=h, image_status=status,
              image_flags=jd(flags), reviewer=reviewer)
    if status == "approved":
        audit(p["id"], f"{table}_image", row["id"], "approve", reviewer)


def _enqueue_tts(p: dict, ch: dict, seg: dict) -> None:
    s = p["settings"]
    h = prompts.text_hash(seg["narration"], s["tts_voice"], s["tts_rate"], s["tts_model"], seg["audio_regen"])
    reuse = assets.find_by_input_hash(h, "audio")
    if reuse:
        meta = jl(reuse["meta_json"], {})
        _set_audio_result(p, ch, seg, reuse["id"], h, float(meta.get("duration", 0)), meta.get("sentences", []))
        return
    payload = TtsPayload(text=seg["narration"], voice=s["tts_voice"], rate=float(s["tts_rate"]),
                         language=s["language"], meta={"chapter": ch["idx"], "segment": seg["idx"]})
    jobs.enqueue(p["id"], "tts.synthesize", payload.model_dump(), "segment_audio", seg["id"], input_hash=h,
                 model_hint=s["tts_model"], priority=2)
    db.update("segment", seg["id"], audio_status="generating")


def _set_audio_result(p: dict, ch: dict, seg: dict, asset_id: int, h: str, duration: float, sentences: list) -> None:
    flags = checks.audio_flags(seg["narration"], duration)
    if seg["audio_regen"]:
        flags.append("regenerated")
    status, reviewer = gate_status(p, ch, "audio", flags, f"audio:{seg['id']}:{seg['audio_regen']}")
    db.update("segment", seg["id"], audio_asset_id=asset_id, audio_hash=h, audio_status=status,
              audio_flags=jd(flags), duration=duration, sentences_json=jd(sentences), reviewer=reviewer)
    if status == "approved":
        audit(p["id"], "segment_audio", seg["id"], "approve", reviewer)


# ================================================================ produce
def produce(p: dict, ch: dict) -> None:
    s = p["settings"]
    beats = {b["id"]: b for b in db.q("SELECT * FROM beat WHERE chapter_id=? AND status='approved'", ch["id"])}
    if wants_video(p):
        w, h = int(s["video_width"]), int(s["video_height"])
        for seg in db.q("SELECT * FROM segment WHERE chapter_id=? ORDER BY idx", ch["id"]):
            b = beats.get(seg["beat_id"])
            if not b:
                continue
            if seg["image_status"] == "missing":
                _enqueue_scene(p, ch, "segment", seg, b, w, h)
            if seg["audio_status"] == "missing":
                _enqueue_tts(p, ch, seg)
    if wants_comic(p):
        for pn in db.q("SELECT * FROM panel WHERE chapter_id=? ORDER BY page_id, slot", ch["id"]):
            b = beats.get(pn["beat_id"])
            if b and pn["image_status"] == "missing":
                _enqueue_scene(p, ch, "panel", pn, b, pn["width"], pn["height"])
    check_done(p, ch)


def chapter_progress(p: dict, ch: dict) -> dict:
    out = {"images": 0, "images_ok": 0, "audio": 0, "audio_ok": 0, "panels": 0, "panels_ok": 0}
    if wants_video(p):
        r = db.one("""SELECT COUNT(*) n, SUM(image_status='approved') i, SUM(audio_status='approved') a
                      FROM segment WHERE chapter_id=?""", ch["id"])
        out.update(images=r["n"] or 0, images_ok=r["i"] or 0, audio=r["n"] or 0, audio_ok=r["a"] or 0)
    if wants_comic(p):
        r = db.one("SELECT COUNT(*) n, SUM(image_status='approved') i FROM panel WHERE chapter_id=?", ch["id"])
        out.update(panels=r["n"] or 0, panels_ok=r["i"] or 0)
    return out


def check_done(p: dict, ch: dict) -> None:
    pr = chapter_progress(p, ch)
    ok = True
    if wants_video(p):
        ok &= pr["images"] > 0 and pr["images_ok"] == pr["images"] and pr["audio_ok"] == pr["audio"]
    if wants_comic(p):
        ok &= pr["panels"] > 0 and pr["panels_ok"] == pr["panels"]
    if not ok:
        return
    db.update("chapter", ch["id"], status="done")
    status, reviewer = gate_status(p, ch, "publish", [], f"publish:{ch['id']}")
    if status == "approved":
        audit(p["id"], "chapter", ch["id"], "publish", reviewer)
        publish(p, ch)


def publish(p: dict, ch: dict) -> None:
    if wants_video(p):
        jobs.enqueue(p["id"], "local.render_video", {"chapter_id": ch["id"]}, "render_video", ch["id"], priority=0)
    if wants_comic(p):
        jobs.enqueue(p["id"], "local.compose_comic", {"chapter_id": ch["id"]}, "compose_comic", ch["id"], priority=0)


# ================================================================ advance
def request_run(chapter_id: int) -> None:
    db.update("chapter", chapter_id, run_requested=1)
    advance_chapter(chapter_id)


def can_start(p: dict, ch: dict) -> bool:
    return bool(p["autorun"] or ch["run_requested"])


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
        if not can_start(p, ch):
            return
        prev = db.one("SELECT status FROM chapter WHERE project_id=? AND idx<? ORDER BY idx DESC LIMIT 1",
                      p["id"], ch["idx"])
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
    db.ex("""UPDATE chapter SET needs_recheck=1 WHERE project_id=? AND idx>? AND status NOT IN ('new','extracting')""",
          pid, chapter_idx)


def advance_producing(pid: int) -> None:
    for ch in db.q("SELECT id FROM chapter WHERE project_id=? AND status IN ('producing','review_beats') ORDER BY idx", pid):
        advance_chapter(ch["id"])


# ================================================================ job results
def _artifact(job: dict, name: str) -> Path:
    path = jobs.artifact_dir(job["id"]) / Path(name).name
    if not path.exists():
        raise FileNotFoundError(f"Thiếu artifact {name} của job {job['id']}")
    return path


def handle_ref_image(job: dict, output: dict) -> None:
    r = db.get("ref", job["owner_id"])
    if not r:
        return
    p = project(r["project_id"])
    aid = assets.save_file(_artifact(job, output["files"][0]), "ref", p["id"], input_hash=job["input_hash"],
                           model_id=job["model_id"], meta={"label": r["label"]})
    _set_ref_result(p, r, aid, job["input_hash"], regenerated=bool(r["regen_count"]))
    advance_producing(p["id"])


def handle_scene_image(table: str, job: dict, output: dict) -> None:
    row = db.get(table, job["owner_id"])
    if not row:
        return
    ch = db.get("chapter", row["chapter_id"])
    p = project(ch["project_id"])
    aid = assets.save_file(_artifact(job, output["files"][0]), "scene", p["id"], input_hash=job["input_hash"],
                           model_id=job["model_id"], meta={"table": table})
    _set_scene_result(p, ch, table, db.get(table, row["id"]), aid, job["input_hash"])
    advance_chapter(ch["id"])


def handle_tts(job: dict, output: dict) -> None:
    seg = db.get("segment", job["owner_id"])
    if not seg:
        return
    ch = db.get("chapter", seg["chapter_id"])
    p = project(ch["project_id"])
    dur = float(output.get("duration") or 0)
    sentences = output.get("sentences") or []
    aid = assets.save_file(_artifact(job, output["file"]), "audio", p["id"], input_hash=job["input_hash"],
                           model_id=job["model_id"], meta={"duration": dur, "sentences": sentences})
    _set_audio_result(p, ch, seg, aid, job["input_hash"], dur, sentences)
    advance_chapter(ch["id"])


def handle_render_video(job: dict, output: dict) -> None:
    db.update("chapter", job["owner_id"], video_asset_id=output.get("video_asset_id"),
              srt_asset_id=output.get("srt_asset_id"))


def handle_compose_comic(job: dict, output: dict) -> None:
    db.update("chapter", job["owner_id"], comic_cbz_asset_id=output.get("cbz_asset_id"),
              comic_pdf_asset_id=output.get("pdf_asset_id"))


HANDLERS = {
    "chapter_extract": handle_extract,
    "chapter_beats": handle_beats,
    "ref_image": handle_ref_image,
    "segment_image": lambda j, o: handle_scene_image("segment", j, o),
    "panel_image": lambda j, o: handle_scene_image("panel", j, o),
    "segment_audio": handle_tts,
    "render_video": handle_render_video,
    "compose_comic": handle_compose_comic,
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
    if ot in ("chapter_extract", "chapter_beats", "render_video", "compose_comic"):
        db.update("chapter", oid, status="error" if ot.startswith("chapter_") else db.val(
            "SELECT status FROM chapter WHERE id=?", oid), error=err)
    elif ot == "ref_image":
        db.update("ref", oid, status="failed")
    elif ot in ("segment_image", "panel_image"):
        db.update(ot.split("_")[0], oid, image_status="failed")
    elif ot == "segment_audio":
        db.update("segment", oid, audio_status="failed")


# ================================================================ regenerate
def regen_ref(rid: int, new_seed: bool = True) -> None:
    r = db.get("ref", rid)
    if not r:
        return
    db.update("ref", rid, status="missing", seed=r["seed"] + (1 if new_seed else 0), regen_count=r["regen_count"] + 1,
              locked=0)
    # tao lai thi cac anh dung tham chieu nay se thanh "cu"
    advance_producing(r["project_id"])
    r = db.get("ref", rid)
    if r["status"] == "missing":
        _requeue_ref(r)


def _requeue_ref(r: dict) -> None:
    p = project(r["project_id"])
    refs = [(a, "reference") for a in jl(r["refs_json"], [])]
    ensure_ref(p, r["owner_type"], r["owner_id"], r["state_key"], r["label"], r["prompt"], refs, r["width"], r["height"])


def _reopen_chapter(chapter_id: int) -> None:
    ch = db.get("chapter", chapter_id)
    if ch and ch["status"] == "done":
        db.update("chapter", chapter_id, status="producing")


def regen_scene(table: str, row_id: int, new_seed: bool = True) -> None:
    row = db.get(table, row_id)
    if not row:
        return
    db.update(table, row_id, image_status="missing", seed=row["seed"] + (1 if new_seed else 0),
              image_regen=row["image_regen"] + 1)
    _reopen_chapter(row["chapter_id"])
    advance_chapter(row["chapter_id"])


def regen_audio(seg_id: int) -> None:
    seg = db.get("segment", seg_id)
    if not seg:
        return
    db.update("segment", seg_id, audio_status="missing", audio_regen=seg["audio_regen"] + 1)
    _reopen_chapter(seg["chapter_id"])
    advance_chapter(seg["chapter_id"])


def is_stale(p: dict, ch: dict, table: str, row: dict) -> bool:
    if not row["image_asset_id"]:
        return False
    beat = db.get("beat", row["beat_id"])
    if not beat:
        return False
    s = p["settings"]
    w, h = (int(s["video_width"]), int(s["video_height"])) if table == "segment" else (row["width"], row["height"])
    spec = scene_spec(p, ch, beat, w, h, row["seed"], ensure=False)
    return spec is not None and spec["hash"] != row["image_hash"]


def regen_stale(chapter_id: int) -> int:
    ch = db.get("chapter", chapter_id)
    p = project(ch["project_id"])
    n = 0
    for table in ("segment", "panel"):
        for row in db.q(f"SELECT * FROM {table} WHERE chapter_id=?", chapter_id):
            if is_stale(p, ch, table, row):
                db.update(table, row["id"], image_status="missing")
                n += 1
    if n:
        _reopen_chapter(ch["id"])
    advance_chapter(chapter_id)
    return n


def reset_chapter(chapter_id: int, to: str) -> None:
    """Chay lai tu mot buoc: 'extract' hoac 'beats'."""
    ch = db.get("chapter", chapter_id)
    with db.tx():
        if to == "extract":
            db.ex("DELETE FROM beat WHERE chapter_id=?", chapter_id)
            db.ex("DELETE FROM page WHERE chapter_id=?", chapter_id)
            db.update("chapter", chapter_id, status="new", run_requested=1, error="", needs_recheck=0)
        else:
            db.ex("DELETE FROM beat WHERE chapter_id=?", chapter_id)
            db.ex("DELETE FROM page WHERE chapter_id=?", chapter_id)
            db.update("chapter", chapter_id, status="state_ready", error="")
    for j in db.q("SELECT id FROM job WHERE owner_type IN ('chapter_extract','chapter_beats') AND owner_id=? AND status='queued'", ch["id"]):
        jobs.cancel(j["id"])
    advance_chapter(chapter_id)


# ================================================================ local jobs
def run_local_job(job: dict) -> dict:
    cfg = get_settings()
    payload = jl(job["payload_json"], {})
    ch = db.get("chapter", payload["chapter_id"])
    p = project(ch["project_id"])
    s = p["settings"]
    work = jobs.artifact_dir(job["id"])
    if job["kind"] == "local.render_video":
        segs = db.q("SELECT * FROM segment WHERE chapter_id=? ORDER BY idx", ch["id"])
        items, srt, t = [], [], 0.0
        for sg in segs:
            img = db.get("asset", sg["image_asset_id"]) if sg["image_asset_id"] else None
            aud = db.get("asset", sg["audio_asset_id"]) if sg["audio_asset_id"] else None
            dur = sg["duration"] or video.estimate_duration(sg["narration"])
            items.append({"image": assets.abs_path(img) if img else None,
                          "audio": assets.abs_path(aud) if aud else None, "duration": dur})
            sents = jl(sg["sentences_json"], [])
            if sents:
                srt += [{"start": t + x["start"], "end": t + x["end"], "text": x["text"]} for x in sents]
            else:
                srt.append({"start": t, "end": t + dur, "text": sg["narration"]})
            t += dur
        out = video.render(items, work, cfg.ffmpeg, int(s["video_width"]), int(s["video_height"]),
                           int(s["video_fps"]), s["video_encoder"], float(s["video_zoom"]))
        vid = assets.save_file(out, "video", p["id"], meta={"chapter": ch["idx"]})
        srt_path = work / "chapter.srt"
        srt_path.write_text(video.build_srt(srt), encoding="utf-8")
        sid = assets.save_file(srt_path, "subtitle", p["id"], meta={"chapter": ch["idx"]})
        return {"video_asset_id": vid, "srt_asset_id": sid}
    if job["kind"] == "local.compose_comic":
        font = comic.find_font(cfg.font_path)
        pw, ph = int(s["comic_page_width"]), int(s["comic_page_height"])
        files = []
        for pg in db.q("SELECT * FROM page WHERE chapter_id=? ORDER BY idx", ch["id"]):
            panels = db.q("SELECT * FROM panel WHERE page_id=? ORDER BY slot", pg["id"])
            imgs, balloons = {}, {}
            for pn in panels:
                a = db.get("asset", pn["image_asset_id"]) if pn["image_asset_id"] else None
                imgs[pn["id"]] = assets.abs_path(a) if a else None
                balloons[pn["id"]] = db.q("SELECT * FROM balloon WHERE panel_id=? ORDER BY idx", pn["id"])
            img = comic.compose_page(pg["layout"], pw, ph, panels, imgs, balloons, font)
            f = work / f"page_{pg['idx'] + 1:03d}.png"
            img.save(f, "PNG")
            aid = assets.save_file(f, "page", p["id"], meta={"chapter": ch["idx"], "page": pg["idx"]})
            db.update("page", pg["id"], asset_id=aid)
            files.append(assets.abs_path(db.get("asset", aid)))
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
        out = run_local_job(job)
        jobs.complete(job["id"], out, "local", now() - t0)
    except Exception as e:  # noqa: BLE001
        log.exception("Local job %s lỗi", job["id"])
        jobs.fail(job["id"], str(e), retryable=False)
    finally:
        shutil.rmtree(jobs.artifact_dir(job["id"]), ignore_errors=True)
    return True
