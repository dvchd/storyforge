"""Gợi ý và gộp nhân vật / bối cảnh bị trùng (do viết tắt, biệt danh, lỗi chính tả).

Hai công tắc độc lập (cài đặt dự án):
- dedupe_fuzzy: tự gợi ý khi tên gần giống (ngưỡng dedupe_threshold, mặc định 0.90)
- dedupe_llm: hiện nút "Nhờ LLM rà trùng" (hợp với truyện nhiều người trùng tên con)
Dự án cũ dùng dedupe_mode được tự đổi sang hai công tắc này (xem engine.settings_of).
"""
from __future__ import annotations

from . import checks, engine, jobs, llm
from .db import db, jd, jl


def scan(pid: int) -> int:
    p = engine.project(pid)
    if not engine.dedupe_fuzzy_on(p["settings"]):
        return 0
    thr = float(p["settings"]["dedupe_threshold"])
    before = db.val("SELECT COUNT(*) FROM suggestion WHERE project_id=?", pid) or 0
    for kind, rows in (("merge_character", engine.all_chars(pid)), ("merge_location", engine.locs(pid))):
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                score = max(checks.name_similarity(x, y) for x in [a["name"], *a["aliases"]] for y in [b["name"], *b["aliases"]])
                if score >= thr:
                    keep, merge = (a, b) if (a["first_chapter"], a["id"]) <= (b["first_chapter"], b["id"]) else (b, a)
                    engine.add_suggestion(pid, kind, keep["id"], merge["id"], score, "Tên gần giống", "fuzzy")
    return (db.val("SELECT COUNT(*) FROM suggestion WHERE project_id=?", pid) or 0) - before


def enqueue_llm(pid: int) -> int:
    p = engine.project(pid)
    if not engine.dedupe_llm_on(p["settings"]):
        return 0
    chars, locs = engine.all_chars(pid), engine.locs(pid)
    payload = engine._llm_payload(p, llm.dedupe_messages(chars, locs), llm.DedupeOut, "DedupeOut", {
        "task": "dedupe", "characters": [{"name": c["name"], "aliases": c["aliases"]} for c in chars],
        "locations": [{"name": l["name"], "aliases": l["aliases"]} for l in locs]})
    return jobs.enqueue(pid, "llm.chat", payload, "project_dedupe", pid, model_hint=p["settings"]["llm_model"], priority=3)


def apply_llm(pid: int, data: llm.DedupeOut) -> int:
    n = 0
    for kind, rows, pairs in (("merge_character", engine.all_chars(pid), data.characters),
                              ("merge_location", engine.locs(pid), data.locations)):
        idx = engine._index(rows)
        for pr in pairs:
            a, b = idx.get(checks.norm(pr.keep)), idx.get(checks.norm(pr.merge))
            if a and b and a["id"] != b["id"]:
                engine.add_suggestion(pid, kind, a["id"], b["id"], 0.95, pr.reason or "LLM nhận định trùng", "llm")
                n += 1
    return n


def open_suggestions(pid: int) -> list[dict]:
    out = []
    for s in db.q("SELECT * FROM suggestion WHERE project_id=? AND status='open' ORDER BY score DESC, id", pid):
        table = "character" if s["kind"] == "merge_character" else "location"
        a, b = db.get(table, s["a_id"]), db.get(table, s["b_id"])
        if not a or not b:
            db.update("suggestion", s["id"], status="dismissed")
            continue
        out.append({**s, "a": a, "b": b, "table": table})
    return out


def _replace_in_cast(pid: int, src: int, dst: int, dst_name: str) -> None:
    for b in db.q("SELECT id, cast_json FROM beat WHERE project_id=?", pid):
        cast = jl(b["cast_json"], [])
        if not any(c.get("character_id") == src for c in cast):
            continue
        seen, uniq = set(), []
        for c in cast:
            if c.get("character_id") == src:
                c["character_id"], c["name"] = dst, dst_name
            if c["character_id"] not in seen:
                seen.add(c["character_id"])
                uniq.append(c)
        db.update("beat", b["id"], cast_json=jd(uniq))


def merge(kind: str, keep_id: int, merge_id: int, reviewer: str = "user") -> dict:
    table = "character" if kind == "merge_character" else "location"
    keep, src = db.get(table, keep_id), db.get(table, merge_id)
    if not keep or not src or keep_id == merge_id:
        raise ValueError("Không gộp được")
    pid = keep["project_id"]
    with db.tx():
        aliases = list(dict.fromkeys([*jl(keep["aliases_json"], []), src["name"], *jl(src["aliases_json"], [])]))
        aliases = [a for a in aliases if checks.norm(a) != checks.norm(keep["name"])]
        cols = {"aliases_json": jd(aliases), "first_chapter": min(keep["first_chapter"], src["first_chapter"])}
        if table == "character" and not keep["appearance"] and src["appearance"]:
            cols["appearance"] = src["appearance"]
        if table == "location" and not keep["description"] and src["description"]:
            cols["description"] = src["description"]
        db.update(table, keep_id, **cols)
        db.ex("UPDATE state_event SET subject_id=? WHERE subject_type=? AND subject_id=?", keep_id, table, merge_id)
        if table == "character":
            _replace_in_cast(pid, merge_id, keep_id, keep["name"])
            db.ex("UPDATE balloon SET character_id=? WHERE character_id=?", keep_id, merge_id)
            db.ex("DELETE FROM ref WHERE owner_type IN ('character_face','character_outfit') AND owner_id=?", merge_id)
        else:
            db.ex("UPDATE beat SET location_id=? WHERE location_id=?", keep_id, merge_id)
            db.ex("DELETE FROM ref WHERE owner_type='location' AND owner_id=?", merge_id)
        db.delete(table, merge_id)
        db.ex("UPDATE suggestion SET status='merged' WHERE kind=? AND ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?))",
              kind, keep_id, merge_id, merge_id, keep_id)
        db.ex("UPDATE suggestion SET status='dismissed' WHERE kind=? AND status='open' AND (a_id=? OR b_id=?)",
              kind, merge_id, merge_id)
    engine.audit(pid, table, keep_id, "merge", reviewer, f"gộp {src['name']}")
    first = min(keep["first_chapter"], src["first_chapter"])
    engine.mark_recheck_after(pid, first - 1)
    engine.advance_project(pid)
    engine.refresh_refs(pid, first)
    return db.get(table, keep_id)


def dismiss(sid: int) -> None:
    db.update("suggestion", sid, status="dismissed")
