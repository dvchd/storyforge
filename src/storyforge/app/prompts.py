"""Ghep prompt anh tu du lieu co cau truc.

Cung mot nhan vat o cung mot trang thai luon duoc mo ta bang dung mot chuoi chu.
"""
from __future__ import annotations

import hashlib
import json

from . import state as st

SHOT_TEXT = {"wide": "wide establishing shot", "medium": "medium shot", "close": "close-up shot"}


def face_ref_prompt(style: str, char: dict, state: dict) -> str:
    face = st.describe(state, st.FACE_FIELDS)
    return (f"{style}. Character reference portrait of {char['name']}, head and shoulders, front view, "
            f"neutral expression, plain light grey background, even lighting. "
            f"Appearance: {char.get('appearance') or 'unspecified'}." + (f" {face}." if face else ""))


def outfit_ref_prompt(style: str, char: dict, state: dict) -> str:
    look = st.describe(state, st.OUTFIT_FIELDS)
    return (f"{style}. Full body character reference sheet of {char['name']}, standing, front view, "
            f"plain light grey background. Reference image 1 is the face of {char['name']}, keep the same face. "
            f"Appearance: {char.get('appearance') or 'unspecified'}." + (f" {look}." if look else ""))


def location_ref_prompt(style: str, loc: dict) -> str:
    variant = "" if loc["variant"] in ("", "default") else f", {loc['variant']}"
    return (f"{style}. Establishing background of {loc['name']}: {loc.get('description') or loc['name']}"
            f"{variant}. No people, no text.")


def scene_prompt(style: str, beat: dict, cast: list[dict], loc: dict | None, ref_roles: list[str]) -> str:
    who = "; ".join(
        f"{c['name']} ({c.get('appearance', '')}"
        + (f", {c['state_text']}" if c.get("state_text") else "") + ")"
        + (f", {c['pose']}" if c.get("pose") else "")
        + (f", {c['expression']} expression" if c.get("expression") else "")
        for c in cast)
    parts = [style, SHOT_TEXT.get(beat.get("shot") or "medium", "medium shot")]
    if loc:
        parts.append(f"Setting: {loc['name']}" + ("" if loc["variant"] in ("", "default") else f" ({loc['variant']})"))
    if who:
        parts.append(f"Characters: {who}")
    if beat.get("action"):
        parts.append(f"Action: {beat['action']}")
    if beat.get("mood"):
        parts.append(f"Mood: {beat['mood']}")
    roles = " ".join(f"Reference image {i} is {r}." for i, r in enumerate(ref_roles, 1))
    if roles:
        parts.append(roles + " Keep faces and outfits consistent with the references.")
    parts.append("No text, no speech bubbles, no watermark.")
    return ". ".join(p.strip().rstrip(".") for p in parts if p) + "."


def input_hash(prompt: str, ref_shas: list[str], seed: int, model: str, width: int, height: int,
               extra: str = "") -> str:
    blob = json.dumps([prompt, ref_shas, seed, model, width, height, extra], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def text_hash(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, default=str).encode()).hexdigest()


def seed_for(*parts: object) -> int:
    return int(hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:8], 16)
