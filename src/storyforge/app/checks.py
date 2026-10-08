"""Kiểm tra tự động, sinh cờ cảnh báo cho cổng duyệt."""
from __future__ import annotations

import difflib
import re
import unicodedata

from . import state as st

_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?…:;])\s+|\n+")


def norm(s: str) -> str:
    """Chuẩn hóa dùng chung MỌI nơi so khớp tên: NFC, chữ thường, ngoặc kép thống nhất, gộp khoảng trắng."""
    s = unicodedata.normalize("NFC", s or "").lower()
    s = (s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
         .replace("\u2026", "..."))
    return _WS.sub(" ", s).strip()


def fold(s: str) -> str:
    """Bỏ dấu tiếng Việt để so tên gần đúng (Lâm An ~ Lam An)."""
    s = unicodedata.normalize("NFD", norm(s)).replace("đ", "d")
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text or "") if s and s.strip()]


def evidence_score(evidence: str, text: str) -> float:
    """0..1: mức câu trích khớp với một câu (hoặc hai câu liền nhau) trong chương.

    So theo từng câu thay vì cả chương để chương dài không làm sai lệch kết quả.
    """
    ev = norm(evidence).strip(' "\'')
    if len(ev) < 6:
        return 0.0
    tx = norm(text)
    if ev in tx:
        return 1.0
    sents = [norm(s) for s in sentences(text)]
    windows = sents + [a + " " + b for a, b in zip(sents, sents[1:])]
    best = 0.0
    for w in windows:
        if len(w) < 0.5 * len(ev):
            continue
        sm = difflib.SequenceMatcher(None, ev, w, autojunk=False)
        # chỉ tính khối khớp từ 4 ký tự để câu dài không cộng dồn ký tự lẻ
        cover = sum(b.size for b in sm.get_matching_blocks() if b.size >= 4) / len(ev)
        best = max(best, min(1.0, cover))
        if best >= 0.99:
            break
    return best


def evidence_found(evidence: str, text: str, threshold: float = 0.9) -> bool:
    return evidence_score(evidence, text) >= threshold


def locate(evidence: str, paras: list[str]) -> int | None:
    """Trả về chỉ số đoạn (1-based) chứa câu trích tốt nhất."""
    best, idx = 0.0, None
    for i, p in enumerate(paras, 1):
        s = evidence_score(evidence, p)
        if s > best:
            best, idx = s, i
    return idx if best >= 0.75 else None


# ------------------------------------------------------------- names
def name_similarity(a: str, b: str) -> float:
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return 0.0
    if fa == fb:
        return 1.0
    ta, tb = fa.split(), fb.split()
    # "An" và "Lâm An", "Tiểu An": một tên là phần đuôi / tập con của tên kia
    if (set(ta) <= set(tb) or set(tb) <= set(ta)) and min(len(ta), len(tb)) >= 1:
        short = ta if len(ta) <= len(tb) else tb
        if len(" ".join(short)) >= 2:
            return 0.86
    # viết tắt: "L. An" ~ "Lâm An"
    if len(ta) == len(tb) and any(x.endswith(".") for x in ta + tb):
        def same(x: str, y: str) -> bool:
            if x.endswith("."):
                return y.startswith(x[:-1])
            if y.endswith("."):
                return x.startswith(y[:-1])
            return x == y
        if all(same(x, y) for x, y in zip(ta, tb)):
            return 0.9
    return difflib.SequenceMatcher(None, fa, fb).ratio()


def best_match(name: str, candidates: dict[str, int]) -> tuple[int | None, float]:
    """candidates: tên/tên gọi khác -> id. Trả id gần nhất và điểm."""
    best_id, best = None, 0.0
    for cand, cid in candidates.items():
        s = name_similarity(name, cand)
        if s > best:
            best_id, best = cid, s
    return best_id, best


# ------------------------------------------------------------- events
def event_flags(field: str, value: str, permanent: bool, evidence: str, chapter_text: str,
                current: dict[str, str]) -> list[str]:
    flags: list[str] = []
    if not evidence_found(evidence, chapter_text):
        flags.append("evidence_missing")
    if permanent or field == "mark":
        flags.append("permanent")
    if not value and field not in current:
        flags.append("conflict")
    if value and current.get(field) == value:
        flags.append("redundant")
    if field == "age" and value and current.get("age"):
        a, b = st.age_number(current["age"]), st.age_number(value)
        if a is not None and b is not None and b < a:
            flags.append("age_regression")
    return flags


# ------------------------------------------------------------- audio
def syllables(text: str) -> int:
    return max(1, len(re.findall(r"\w+", text or "")))


def audio_flags(text: str, duration: float) -> list[str]:
    """Tiếng Việt đọc bình thường khoảng 0.18 đến 0.45 giây mỗi âm tiết."""
    if duration <= 0.3:
        return ["audio_mismatch"]
    sps = duration / syllables(text)
    return ["audio_mismatch"] if sps < 0.12 or sps > 0.7 else []
