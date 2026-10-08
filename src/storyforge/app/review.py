"""Hành động duyệt của người dùng. Dùng chung một luồng với duyệt tự động."""
from __future__ import annotations

from typing import Any

from . import engine
from .db import db, jl

# entity -> (bảng, cột trạng thái, cột cờ, cột người duyệt)
ENTITIES = {
    "character": ("character", "status", "flags_json", "reviewer"),
    "location": ("location", "status", "flags_json", "reviewer"),
    "event": ("state_event", "status", "flags_json", "reviewer"),
    "beat": ("beat", "status", "flags_json", "reviewer"),
    "ref": ("ref", "status", "flags_json", "reviewer"),
    "beat_image": ("beat", "image_status", "image_flags", "image_reviewer"),
    "segment_audio": ("segment", "audio_status", "audio_flags", "reviewer"),
}
TITLES = {"character": "Nhân vật mới", "location": "Bối cảnh mới", "event": "Sự kiện trạng thái", "beat": "Nhịp",
          "ref": "Ảnh tham chiếu", "beat_image": "Ảnh cảnh", "segment_audio": "Giọng đọc"}
CHAPTER_SCOPED = {"event", "beat", "beat_image", "segment_audio"}


def get(entity: str, id_: int) -> dict | None:
    return db.get(ENTITIES[entity][0], id_)


def set_status(entity: str, id_: int, status: str, reviewer: str, note: str = "") -> dict | None:
    if entity not in ENTITIES or status not in ("approved", "rejected", "pending"):
        raise ValueError("entity/status không hợp lệ")
    table, col, _, rcol = ENTITIES[entity]
    row = db.get(table, id_)
    if not row:
        return None
    pid = row["project_id"]
    db.update(table, id_, **{col: status, rcol: reviewer})
    engine.audit(pid, entity, id_, {"approved": "approve", "rejected": "reject", "pending": "reopen"}[status],
                 reviewer, note)
    _after(entity, row, status)
    return db.get(table, id_)


def _after(entity: str, row: dict, status: str) -> None:
    pid = row["project_id"]
    s = engine.project(pid)["settings"]
    auto_regen = bool(s.get("auto_regen_on_reject")) and status == "rejected"
    max_regen = int(s.get("max_regen", 3))
    if entity == "character":
        if status == "rejected":
            db.ex("UPDATE state_event SET status='rejected', reviewer='cascade' WHERE subject_type='character' "
                  "AND subject_id=? AND status='pending'", row["id"])
        engine.advance_project(pid)
    elif entity == "location":
        if status == "rejected":
            db.ex("UPDATE state_event SET status='rejected', reviewer='cascade' WHERE subject_type='location' "
                  "AND subject_id=? AND status='pending'", row["id"])
        engine.advance_project(pid)
    elif entity == "event":
        engine.mark_recheck_after(pid, row["chapter_idx"])
        engine.advance_chapter(row["chapter_id"])
        engine.refresh_refs(pid, row["chapter_idx"])
    elif entity == "beat":
        engine.advance_chapter(row["chapter_id"])
    elif entity == "ref":
        if auto_regen and row["regen_count"] < max_regen:
            engine.regen_ref(row["id"])
        else:
            engine.advance_producing(pid)
    elif entity == "beat_image":
        if status != "approved":
            db.ex("UPDATE chapter SET status='producing' WHERE id=? AND status='done'", row["chapter_id"])
        if auto_regen and row["image_regen"] < max_regen:
            engine.regen_beat_image(row["id"])
        else:
            engine.advance_chapter(row["chapter_id"])
    elif entity == "segment_audio":
        if status != "approved":
            db.ex("UPDATE chapter SET status='producing' WHERE id=? AND status='done'", row["chapter_id"])
        if auto_regen and row["audio_regen"] < max_regen:
            engine.regen_audio(row["id"])
        else:
            engine.advance_chapter(row["chapter_id"])


def pending_query(entity: str, project_id: int, chapter_id: int | None) -> tuple[str, list[Any]]:
    table, col, _, _ = ENTITIES[entity]
    sql = f"SELECT * FROM {table} WHERE project_id=? AND {col}='pending'"
    args: list[Any] = [project_id]
    if chapter_id:
        ch = db.get("chapter", chapter_id)
        if entity in CHAPTER_SCOPED:
            sql += " AND chapter_id=?"
            args.append(chapter_id)
        elif ch:
            sql += " AND first_chapter<=?"
            args.append(ch["idx"])
    return sql, args


def bulk(entity: str, chapter_id: int | None, project_id: int, status: str, reviewer: str) -> int:
    sql, args = pending_query(entity, project_id, chapter_id)
    rows = db.q(sql.replace("SELECT *", "SELECT id"), *args)
    for r in rows:
        set_status(entity, r["id"], status, reviewer)
    return len(rows)


def pending_counts(project_id: int) -> dict[str, int]:
    """Một câu truy vấn cho mọi loại (gọi ở mọi trang)."""
    parts, args = [], []
    for ent, (table, col, _, _) in ENTITIES.items():
        parts.append(f"SELECT '{ent}' e, COUNT(*) n FROM {table} WHERE project_id=? AND {col}='pending'")
        args.append(project_id)
    out = {r["e"]: r["n"] for r in db.q(" UNION ALL ".join(parts), *args)}
    out["suggestions"] = db.val("SELECT COUNT(*) FROM suggestion WHERE project_id=? AND status='open'", project_id) or 0
    out["total"] = sum(v for k, v in out.items() if k in ENTITIES)
    return out


def flags_of(entity: str, row: dict) -> list[str]:
    return jl(row.get(ENTITIES[entity][2]), []) or []
