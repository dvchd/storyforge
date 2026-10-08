"""Hanh dong duyet cua nguoi dung. Dung chung mot luong voi duyet tu dong."""
from __future__ import annotations

from . import engine
from .db import db, jl

# entity -> (bang, cot trang thai, cot co canh bao)
ENTITIES = {
    "character": ("character", "status", "flags_json"),
    "location": ("location", "status", "flags_json"),
    "event": ("state_event", "status", "flags_json"),
    "beat": ("beat", "status", "flags_json"),
    "ref": ("ref", "status", "flags_json"),
    "segment_image": ("segment", "image_status", "image_flags"),
    "segment_audio": ("segment", "audio_status", "audio_flags"),
    "panel_image": ("panel", "image_status", "image_flags"),
}


def get(entity: str, id_: int) -> dict | None:
    table = ENTITIES[entity][0]
    return db.get(table, id_)


def set_status(entity: str, id_: int, status: str, reviewer: str, note: str = "") -> dict | None:
    if entity not in ENTITIES or status not in ("approved", "rejected", "pending"):
        raise ValueError("entity/status không hợp lệ")
    table, col, _ = ENTITIES[entity]
    row = db.get(table, id_)
    if not row:
        return None
    pid = row["project_id"]
    db.update(table, id_, **{col: status, "reviewer": reviewer})
    engine.audit(pid, entity, id_, {"approved": "approve", "rejected": "reject", "pending": "reopen"}[status],
                 reviewer, note)
    _after(entity, row, status)
    return db.get(table, id_)


def _after(entity: str, row: dict, status: str) -> None:
    pid = row["project_id"]
    p = engine.project(pid)
    s = p["settings"]
    auto_regen = bool(s.get("auto_regen_on_reject")) and status == "rejected"
    max_regen = int(s.get("max_regen", 3))
    if entity == "character":
        if status == "rejected":
            db.ex("UPDATE state_event SET status='rejected', reviewer='cascade' WHERE character_id=? AND status='pending'",
                  row["id"])
        engine.advance_project(pid)
    elif entity == "location":
        engine.advance_project(pid)
    elif entity == "event":
        engine.mark_recheck_after(pid, row["chapter_idx"])
        engine.advance_chapter(row["chapter_id"])
    elif entity == "beat":
        engine.advance_chapter(row["chapter_id"])
    elif entity == "ref":
        if auto_regen and row["regen_count"] < max_regen:
            engine.regen_ref(row["id"])
        else:
            engine.advance_producing(pid)
    elif entity in ("segment_image", "panel_image", "segment_audio") and status != "approved":
        db.ex("UPDATE chapter SET status='producing' WHERE id=? AND status='done'", row["chapter_id"])
    if entity in ("segment_image", "panel_image"):
        table = ENTITIES[entity][0]
        if auto_regen and row["image_regen"] < max_regen:
            engine.regen_scene(table, row["id"])
        else:
            engine.advance_chapter(row["chapter_id"])
    if entity == "segment_audio":
        if auto_regen and row["audio_regen"] < max_regen:
            engine.regen_audio(row["id"])
        else:
            engine.advance_chapter(row["chapter_id"])


def bulk(entity: str, chapter_id: int | None, project_id: int, status: str, reviewer: str) -> int:
    table, col, _ = ENTITIES[entity]
    if entity in ("character", "location"):
        if chapter_id:
            ch = db.get("chapter", chapter_id)
            rows = db.q(f"SELECT id FROM {table} WHERE project_id=? AND first_chapter=? AND {col}='pending'",
                        project_id, ch["idx"])
        else:
            rows = db.q(f"SELECT id FROM {table} WHERE project_id=? AND {col}='pending'", project_id)
    elif entity == "ref":
        rows = db.q(f"SELECT id FROM ref WHERE project_id=? AND status='pending'", project_id)
    elif chapter_id:
        rows = db.q(f"SELECT id FROM {table} WHERE chapter_id=? AND {col}='pending'", chapter_id)
    else:
        rows = db.q(f"SELECT id FROM {table} WHERE project_id=? AND {col}='pending'", project_id)
    for r in rows:
        set_status(entity, r["id"], status, reviewer)
    return len(rows)


def pending_counts(project_id: int) -> dict[str, int]:
    out = {}
    for ent, (table, col, _) in ENTITIES.items():
        out[ent] = db.val(f"SELECT COUNT(*) FROM {table} WHERE project_id=? AND {col}='pending'", project_id) or 0
    out["total"] = sum(out.values())
    return out


def flags_of(entity: str, row: dict) -> list[str]:
    return jl(row.get(ENTITIES[entity][2]), []) or []
