"""Tính trạng thái nhân vật và bối cảnh theo chương từ các sự kiện đã duyệt.

Trạng thái không lưu cố định: sửa một sự kiện ở chương k thì mọi chương sau tự tính lại.

Phân loại trường của nhân vật:
- Ảnh tham chiếu MẶT phụ thuộc: age, face, mark (dấu vết vĩnh viễn: sẹo, hình xăm...).
- Ảnh tham chiếu TOÀN THÂN phụ thuộc thêm: outfit, hair, accessory, body.
- Chỉ đưa vào prompt (không cần ảnh tham chiếu mới): injury (vết thương tạm thời), other.

Thương tích có permanent=True được chuẩn hóa thành 'mark' để đổi ảnh mặt ở mọi chương sau.
'mark' là trường cộng dồn: nhiều sẹo nối với nhau; giá trị rỗng xóa toàn bộ.
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

FIELD_ALIASES = {
    "clothes": "outfit", "clothing": "outfit", "costume": "outfit", "dress": "outfit", "armor": "outfit",
    "hairstyle": "hair", "wound": "injury", "injuries": "injury",
    "scar": "mark", "scars": "mark", "tattoo": "mark", "marks": "mark", "birthmark": "mark",
    "accessories": "accessory", "item": "accessory", "weapon": "accessory",
    "appearance": "face", "physique": "body", "build": "body",
}

# Bối cảnh: trạng thái lâu dài (bị phá hủy, trang trí lễ hội...). Biến thể ánh sáng/thời tiết
# (night, rain...) là của từng nhịp, không phải trạng thái.
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
            if v:
                if v not in marks:
                    marks.append(v)
            else:
                marks = []
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
        state["mark"] = "; ".join(marks)
    return state


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


def _key(state: dict, fields: tuple[str, ...], extra: str = "") -> str:
    parts = [f"{f}={state[f]}" for f in fields if state.get(f)]
    if extra:
        parts.append(extra)
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
