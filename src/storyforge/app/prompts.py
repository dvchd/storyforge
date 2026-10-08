"""Ghép prompt ảnh từ dữ liệu có cấu trúc.

Cùng một nhân vật ở cùng một trạng thái luôn được mô tả bằng đúng một chuỗi chữ,
nên hash đầu vào ổn định và ảnh được dùng lại khi không có gì thay đổi.
"""
from __future__ import annotations

import difflib
import hashlib
import html
import json

from . import state as st

SHOT_TEXT = {"wide": "wide establishing shot", "medium": "medium shot", "close": "close-up shot"}
# Gợi ý bố cục để ảnh gốc cắt được cả khung video lẫn khung truyện mà không mất đầu.
SHOT_COMPOSITION = {
    "wide": "Keep important subjects near the center of the frame",
    "medium": "Keep characters near the center, heads in the upper half with headroom",
    "close": "Face in the upper third of the frame, whole head visible with headroom above",
}
VARIANT_TEXT = {"default": "", "day": "daytime", "dawn": "at dawn, soft early light", "dusk": "at dusk, warm low sun",
                "night": "at night, moonlight", "rain": "in the rain, wet surfaces", "snow": "in snow",
                "fog": "in thick fog"}


def variant_text(v: str) -> str:
    v = (v or "default").strip().lower()
    return VARIANT_TEXT.get(v, v)


def face_ref_prompt(style: str, char: dict, state: dict) -> str:
    face = st.describe(state, st.FACE_FIELDS)
    return (f"{style}. Character reference portrait of {char['name']}, head and shoulders, front view, "
            f"neutral expression, plain light grey background, even lighting. "
            f"Appearance: {char.get('appearance') or 'unspecified'}." + (f" {face}." if face else ""))


def outfit_ref_prompt(style: str, char: dict, state: dict) -> str:
    look = st.describe(state, st.OUTFIT_FIELDS + st.FACE_FIELDS)
    return (f"{style}. Full body character reference sheet of {char['name']}, standing, front view, "
            f"plain light grey background. Reference image 1 is the face of {char['name']}, keep the same face. "
            f"Appearance: {char.get('appearance') or 'unspecified'}." + (f" {look}." if look else ""))


def location_ref_prompt(style: str, loc: dict, state: dict, variant: str) -> str:
    cond = st.describe(state, st.LOC_FIELDS)
    vt = variant_text(variant)
    return (f"{style}. Establishing background of {loc['name']}: {loc.get('description') or loc['name']}"
            + (f", {vt}" if vt else "") + (f". Current state: {cond}" if cond else "") + ". No people, no text.")


def scene_prompt(style: str, beat: dict, cast: list[dict], loc: dict | None, loc_state: dict,
                 ref_roles: list[str]) -> str:
    who = "; ".join(
        f"{c['name']} ({c.get('appearance', '')}"
        + (f", {c['state_text']}" if c.get("state_text") else "") + ")"
        + (f", {c['pose']}" if c.get("pose") else "")
        + (f", {c['expression']} expression" if c.get("expression") else "")
        for c in cast)
    shot = beat.get("shot") or "medium"
    parts = [style, SHOT_TEXT.get(shot, "medium shot")]
    if loc:
        vt = variant_text(beat.get("variant", "default"))
        cond = st.describe(loc_state, st.LOC_FIELDS)
        parts.append(f"Setting: {loc['name']}" + (f", {vt}" if vt else "") + (f" ({cond})" if cond else ""))
    if who:
        parts.append(f"Characters: {who}")
    if beat.get("action"):
        parts.append(f"Action: {beat['action']}")
    if beat.get("mood"):
        parts.append(f"Mood: {beat['mood']}")
    roles = " ".join(f"Reference image {i} is {r}." for i, r in enumerate(ref_roles, 1))
    if roles:
        parts.append(roles + " Keep faces, marks and outfits consistent with the references")
    parts.append(SHOT_COMPOSITION.get(shot, SHOT_COMPOSITION["medium"]))
    parts.append("No text, no speech bubbles, no watermark")
    return ". ".join(p.strip().rstrip(".") for p in parts if p) + "."


def input_hash(prompt: str, ref_shas: list[str], seed: int, model: str, width: int, height: int,
               extra: str = "") -> str:
    blob = json.dumps([prompt, ref_shas, seed, model, width, height, extra], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def text_hash(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, default=str).encode()).hexdigest()


def seed_for(*parts: object) -> int:
    return int(hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:8], 16)


def word_diff(old: str, new: str) -> str:
    a, b = (old or "").split(), (new or "").split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == "equal":
            out.append(html.escape(" ".join(a[i1:i2])))
        if op in ("delete", "replace"):
            out.append(f"<del>{html.escape(' '.join(a[i1:i2]))}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{html.escape(' '.join(b[j1:j2]))}</ins>")
    return " ".join(out)
