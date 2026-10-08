"""Kiem tra tu dong, sinh co canh bao cho cong duyet."""
from __future__ import annotations

import difflib
import re
import unicodedata

_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").lower()
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    return _WS.sub(" ", s).strip()


def evidence_found(evidence: str, text: str, threshold: float = 0.85) -> bool:
    ev, tx = norm(evidence), norm(text)
    if not ev:
        return False
    if ev in tx:
        return True
    sm = difflib.SequenceMatcher(None, tx, ev, autojunk=False)
    m = sm.find_longest_match(0, len(tx), 0, len(ev))
    return m.size >= threshold * len(ev)


def event_flags(field: str, value: str, permanent: bool, evidence: str,
                chapter_text: str, current: dict[str, str]) -> list[str]:
    flags: list[str] = []
    if not evidence_found(evidence, chapter_text):
        flags.append("evidence_missing")
    if permanent:
        flags.append("permanent")
    if not value and field not in current:
        flags.append("conflict")
    if value and current.get(field) == value:
        flags.append("redundant")
    return flags


def audio_flags(text: str, duration: float) -> list[str]:
    n = max(len(text.strip()), 1)
    spc = duration / n
    if duration <= 0 or spc < 0.03 or spc > 0.25:
        return ["audio_mismatch"]
    return []
