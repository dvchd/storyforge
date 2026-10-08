"""Kiểm tra tự động, sinh cờ cảnh báo cho cổng duyệt."""
from __future__ import annotations

import difflib
import re
import unicodedata

from . import state as st

_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?…:;])\s+|\n+")
_VI_CHARS = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]")


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
    """0..1: mức câu trích khớp một câu (hoặc hai câu liền nhau) trong chương. So theo câu để chương dài không làm sai."""
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
        cover = sum(b.size for b in sm.get_matching_blocks() if b.size >= 4) / len(ev)
        best = max(best, min(1.0, cover))
        if best >= 0.99:
            break
    return best


def evidence_found(evidence: str, text: str, threshold: float = 0.9) -> bool:
    return evidence_score(evidence, text) >= threshold


def locate(evidence: str, paras: list[str]) -> int | None:
    best, idx = 0.0, None
    for i, p in enumerate(paras, 1):
        s = evidence_score(evidence, p)
        if s > best:
            best, idx = s, i
    return idx if best >= 0.75 else None


# ------------------------------------------------------------- tên
def name_similarity(a: str, b: str) -> float:
    """Độ giống tên 0..1.

    - Bỏ dấu khớp tuyệt đối (Lam An ~ Lâm An): 1.0
    - Viết tắt cùng số chữ (L. An ~ Lâm An): 0.9
    - Một tên là tập con của tên kia, CHỈ khi tên ngắn có từ 2 chữ trở lên
      (Lâm An ~ Lâm An Nhiên: 0.86). Tên một chữ (An, Minh) không tự khớp vào tên dài
      vì truyện thường có nhiều người trùng tên con.
    - Còn lại: tỉ lệ giống chuỗi.
    """
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return 0.0
    if fa == fb:
        return 1.0
    ta, tb = fa.split(), fb.split()
    if len(ta) == len(tb) and any(x.endswith(".") for x in ta + tb):
        def same(x: str, y: str) -> bool:
            if x.endswith("."):
                return y.startswith(x[:-1])
            if y.endswith("."):
                return x.startswith(y[:-1])
            return x == y
        if all(same(x, y) for x, y in zip(ta, tb)):
            return 0.9
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) >= 2 and set(short) <= set(long_):
        return 0.86
    if len(short) == 1 or len(long_) == 1:
        return min(0.6, difflib.SequenceMatcher(None, fa, fb).ratio())
    return difflib.SequenceMatcher(None, fa, fb).ratio()


def best_match(name: str, candidates: dict[str, int]) -> tuple[int | None, float]:
    best_id, best = None, 0.0
    for cand, cid in candidates.items():
        s = name_similarity(name, cand)
        if s > best:
            best_id, best = cid, s
    return best_id, best


# ------------------------------------------------------------- ngôn ngữ prompt
def non_english(text: str, names: list[str] | tuple[str, ...] = ()) -> bool:
    """Mô tả cho model ảnh có vẻ đang viết tiếng Việt (bỏ qua tên riêng đã biết)."""
    t = norm(text)
    if not t:
        return False
    for n in sorted({norm(x) for x in names if x}, key=len, reverse=True):
        t = t.replace(n, " ")
    words = re.findall(r"\w+", t)
    if not words:
        return False
    vi = sum(1 for w in words if _VI_CHARS.search(w))
    return vi >= 2 or vi / len(words) >= 0.3


# ------------------------------------------------------------- sự kiện
def event_flags(field: str, value: str, permanent: bool, evidence: str, chapter_text: str,
                current: dict[str, str]) -> list[str]:
    flags: list[str] = []
    if not evidence_found(evidence, chapter_text):
        flags.append("evidence_missing")
    if permanent or field == "mark":
        flags.append("permanent")
    if field == "mark":
        marks = st.marks_of(current)
        if st.is_removal(value):
            if not any(st._same(m, st.removal_target(value)) for m in marks):
                flags.append("conflict")
        elif not value and not marks:
            flags.append("conflict")
        elif value and any(st._same(m, value) for m in marks):
            flags.append("redundant")
        return flags
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
