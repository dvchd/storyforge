"""Tính trạng thái nhân vật và bối cảnh theo chương từ các sự kiện đã duyệt.

Trạng thái không lưu cố định: sửa một sự kiện ở chương k thì mọi chương sau tự tính lại.

Nhân vật:
- Ảnh tham chiếu MẶT phụ thuộc: age, face, mark (dấu vết vĩnh viễn: sẹo, hình xăm...).
- Ảnh tham chiếu TOÀN THÂN phụ thuộc thêm: outfit, hair, accessory, body.
- Chỉ đưa vào prompt: injury (vết thương tạm thời), other.

'mark' là trường cộng dồn, mỗi dấu vết là một mục riêng:
- value "long scar on right cheek"   -> thêm một dấu vết
- value "-long scar on right cheek"  -> xóa đúng dấu vết đó, giữ các dấu vết khác
- value ""                          -> xóa toàn bộ dấu vết
Thương tích có permanent=True được chuẩn hóa thành 'mark'.
"""
from __future__ import annotations

import hashlib
import re

from .db import db

FIELDS = ["outfit", "hair", "injury", "mark", "age", "body", "face", "accessory", "other"]
FIELD_LABELS = {
    "outfit": "Trang phục", "hair": "Tóc", "injury": "Vết thương tạm", "mark": "Dấu vết vĩnh viễn",
    "age": "Tuổi", "body": "Vóc dáng", "face": "Khuôn mặt", "accessory": "Phụ kiện", "other": "Khác",
}
FACE_FIELDS = ("age", "face", "mark")
OUTFIT_FIELDS = ("outfit", "hair", "accessory", "body")
CONDITION_FIELDS = ("injury", "other")
ACCUMULATE = {"mark"}
MARK_SEP = "; "

FIELD_ALIASES = {
    "clothes": "outfit", "clothing": "outfit", "costume": "outfit", "dress": "outfit", "armor": "outfit",
    "hairstyle": "hair", "wound": "injury", "injuries": "injury",
    "scar": "mark", "scars": "mark", "tattoo": "mark", "marks": "mark", "birthmark": "mark",
    "accessories": "accessory", "item": "accessory", "weapon": "accessory",
    "appearance": "face", "physique": "body", "build": "body",
}

LOC_FIELDS = ["condition", "decor", "other"]
LOC_FIELD_LABELS = {"condition": "Hiện trạng", "decor": "Trang trí", "other": "Khác"}
LOC_ALIASES = {"state": "condition", "damage": "condition", "ruin": "condition", "decoration": "decor"}


def normalize_field(f: str, permanent: bool = False, subject: str = "character") -> str:
    f = (f or "").strip().lower()
    if subject == "location":
        f = LOC_ALIASES.get(f, f)
        return f if f in LOC_FIELDS else "other"
    f = FIELD_ALIASES.get(f, f)
    if f not in FIELDS:
        f = "other"
    if f == "injury" and permanent:
        f = "mark"
    return f


def is_removal(value: str) -> bool:
    return (value or "").strip().startswith("-")


def removal_target(value: str) -> str:
    return (value or "").strip().lstrip("-").strip()


def _same(a: str, b: str) -> bool:
    return re.sub(r"\s+", " ", a.strip().lower()) == re.sub(r"\s+", " ", b.strip().lower())


def resolve(events: list[dict], chapter_idx: int) -> dict[str, str]:
    state: dict[str, str] = {}
    marks: list[str] = []
    for e in sorted(events, key=lambda e: (e["chapter_idx"], e["id"])):
        if e["status"] != "approved" or e["chapter_idx"] > chapter_idx:
            continue
        f, v = e["field"], (e["value"] or "").strip()
        expired = e.get("until_chapter") is not None and chapter_idx > e["until_chapter"]
        if f in ACCUMULATE:
            if expired:
                continue
            if not v:
                marks = []
            elif is_removal(v):
                t = removal_target(v)
                marks = [m for m in marks if not _same(m, t)]
            elif not any(_same(m, v) for m in marks):
                marks.append(v)
            continue
        if expired:
            if state.get(f) == v:
                state.pop(f, None)
            continue
        if v:
            state[f] = v
        else:
            state.pop(f, None)
    if marks:
        state["mark"] = MARK_SEP.join(marks)
    return state


def marks_of(state: dict) -> list[str]:
    return [m for m in (state.get("mark") or "").split(MARK_SEP) if m.strip()]


def events_of(subject_type: str, subject_id: int) -> list[dict]:
    return db.q("SELECT * FROM state_event WHERE subject_type=? AND subject_id=?", subject_type, subject_id)


def state_at(character_id: int, chapter_idx: int) -> dict[str, str]:
    return resolve(events_of("character", character_id), chapter_idx)


def loc_state_at(location_id: int, chapter_idx: int) -> dict[str, str]:
    return resolve(events_of("location", location_id), chapter_idx)


def timeline(subject_type: str, subject_id: int, chapters: list[dict]) -> list[dict]:
    evs = events_of(subject_type, subject_id)
    out, prev = [], None
    for ch in chapters:
        s = resolve(evs, ch["idx"])
        out.append({"chapter": ch, "state": s, "changed": s != prev})
        prev = s
    return out


def _key(state: dict, fields: tuple[str, ...]) -> str:
    parts = [f"{f}={state[f]}" for f in fields if state.get(f)]
    if not parts:
        return "base"
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def face_key(state: dict) -> str:
    return _key(state, FACE_FIELDS)


def outfit_key(state: dict) -> str:
    return "o-" + _key(state, OUTFIT_FIELDS + FACE_FIELDS)


def location_key(state: dict, variant: str) -> str:
    v = (variant or "default").strip().lower()
    k = _key(state, tuple(LOC_FIELDS))
    return v if k == "base" else f"{v}-{k}"


def describe(state: dict, fields: tuple[str, ...] | list[str] | None = None) -> str:
    fields = tuple(fields or FIELDS)
    return "; ".join(f"{f}: {state[f]}" for f in fields if state.get(f))


_NUM = re.compile(r"\d+")


def age_number(v: str) -> int | None:
    m = _NUM.search(v or "")
    return int(m.group()) if m else None
