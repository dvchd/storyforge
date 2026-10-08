"""Tinh trang thai nhan vat theo chuong tu cac su kien da duyet.

Trang thai khong luu co dinh: sua mot su kien o chuong k thi moi chuong sau
tu dong tinh lai. Su kien tam thoi het han sau until_chapter.
"""
from __future__ import annotations

import hashlib

from .db import db

FIELDS = ["outfit", "hair", "injury", "age", "body", "face", "accessory", "other"]
FIELD_LABELS = {
    "outfit": "Trang phục", "hair": "Tóc", "injury": "Thương tích", "age": "Tuổi",
    "body": "Vóc dáng", "face": "Khuôn mặt", "accessory": "Phụ kiện", "other": "Khác",
}
# Truong quyet dinh anh tham chieu mat va anh tham chieu toan than.
FACE_FIELDS = ("age", "face")
OUTFIT_FIELDS = ("outfit", "hair", "accessory", "body")
# Chi dua vao prompt, khong can anh tham chieu moi.
CONDITION_FIELDS = ("injury", "other")

FIELD_ALIASES = {
    "clothes": "outfit", "clothing": "outfit", "costume": "outfit", "dress": "outfit",
    "hairstyle": "hair", "wound": "injury", "injuries": "injury", "scar": "injury",
    "accessories": "accessory", "item": "accessory", "weapon": "accessory",
    "appearance": "face", "physique": "body", "build": "body",
}


def normalize_field(f: str) -> str:
    f = (f or "").strip().lower()
    f = FIELD_ALIASES.get(f, f)
    return f if f in FIELDS else "other"


def resolve(events: list[dict], chapter_idx: int) -> dict[str, str]:
    state: dict[str, str] = {}
    for e in sorted(events, key=lambda e: (e["chapter_idx"], e["id"])):
        if e["status"] != "approved" or e["chapter_idx"] > chapter_idx:
            continue
        if e.get("until_chapter") is not None and chapter_idx > e["until_chapter"]:
            # tam thoi da het han: chi xoa neu gia tri hien tai van do su kien nay dat
            if state.get(e["field"]) == e["value"]:
                state.pop(e["field"], None)
            continue
        if e["value"]:
            state[e["field"]] = e["value"]
        else:
            state.pop(e["field"], None)
    return state


def events_of(character_id: int) -> list[dict]:
    return db.q("SELECT * FROM state_event WHERE character_id=?", character_id)


def state_at(character_id: int, chapter_idx: int) -> dict[str, str]:
    return resolve(events_of(character_id), chapter_idx)


def timeline(character_id: int, chapters: list[dict]) -> list[dict]:
    evs = events_of(character_id)
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


def describe(state: dict, fields: tuple[str, ...] | None = None) -> str:
    fields = fields or tuple(FIELDS)
    return "; ".join(f"{f}: {state[f]}" for f in fields if state.get(f))
