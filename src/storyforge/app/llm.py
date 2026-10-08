"""Schema đầu ra LLM và cách dựng prompt cho từng tác vụ.

App tự dựng prompt và tự kiểm tra kết quả. Worker chỉ chạy model.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

SHOTS = ("wide", "medium", "close")


# ------------------------------------------------------------ schemas
class NewCharacter(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    appearance: str = ""
    role: str = ""


class ProposedEvent(BaseModel):
    character: str
    field: str
    value: str = ""
    permanent: bool = False
    lasts_chapters: int | None = None
    evidence: str = ""


class NewLocation(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class LocationEvent(BaseModel):
    location: str
    field: str = "condition"
    value: str = ""
    permanent: bool = False
    lasts_chapters: int | None = None
    evidence: str = ""


class ChapterExtraction(BaseModel):
    summary: str = ""
    new_characters: list[NewCharacter] = Field(default_factory=list)
    events: list[ProposedEvent] = Field(default_factory=list)
    locations: list[NewLocation] = Field(default_factory=list)
    location_events: list[LocationEvent] = Field(default_factory=list)


class CastItem(BaseModel):
    character: str
    pose: str = ""
    expression: str = ""


class DialogueLine(BaseModel):
    character: str = ""
    text: str
    kind: str = "speech"

    @field_validator("kind")
    @classmethod
    def _k(cls, v: str) -> str:
        v = (v or "speech").lower()
        return v if v in ("speech", "thought", "caption") else "speech"


class BeatOut(BaseModel):
    start: int
    end: int
    location: str = ""
    variant: str = "default"
    cast: list[CastItem] = Field(default_factory=list)
    shot: str = "medium"
    action: str = ""
    mood: str = ""
    dialogue: list[DialogueLine] = Field(default_factory=list)

    @field_validator("shot")
    @classmethod
    def _s(cls, v: str) -> str:
        v = (v or "medium").lower()
        for s in SHOTS:
            if s in v:
                return s
        return "close" if "up" in v else "medium"


class BeatsOut(BaseModel):
    beats: list[BeatOut]


class DupPair(BaseModel):
    keep: str
    merge: str
    reason: str = ""


class DedupeOut(BaseModel):
    characters: list[DupPair] = Field(default_factory=list)
    locations: list[DupPair] = Field(default_factory=list)


# ------------------------------------------------------------ helpers
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        return json.loads(text[a:b + 1])
    raise ValueError("Không tìm thấy JSON trong kết quả")


def paragraphs(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").strip()
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in text.split("\n") if p.strip()]
    return parts


def schema_of(model: type[BaseModel]) -> dict:
    return model.model_json_schema()


def span_issues(beats: list[BeatOut], n: int) -> list[str]:
    """Kiểm tra beat phủ kín 1..n, liên tiếp, không chồng lấn."""
    issues = []
    if not beats:
        return ["Danh sách beats rỗng"]
    expect = 1
    for i, b in enumerate(sorted(beats, key=lambda b: (b.start, b.end)), 1):
        if b.end < b.start:
            issues.append(f"beat {i}: end ({b.end}) nhỏ hơn start ({b.start})")
        if b.start > expect:
            issues.append(f"bỏ sót đoạn {expect}..{b.start - 1}")
        elif b.start < expect:
            issues.append(f"beat {i} (đoạn {b.start}..{b.end}) chồng lấn beat trước")
        if b.end > n:
            issues.append(f"beat {i}: end ({b.end}) vượt quá số đoạn ({n})")
        expect = max(expect, b.end + 1)
    if expect <= n:
        issues.append(f"bỏ sót đoạn {expect}..{n} ở cuối chương")
    return issues


# ------------------------------------------------------------ prompts
EXTRACT_SYSTEM = """Bạn là trợ lý biên tập truyện. Nhiệm vụ: theo dõi nhân vật, bối cảnh và thay đổi ngoại hình qua từng chương để vẽ minh họa nhất quán.

Chỉ trả về MỘT đối tượng JSON hợp lệ theo schema, không thêm chữ nào khác.

Quy tắc chung:
- Chỉ ghi điều được nói rõ trong chương. Không suy đoán, không bịa.
- "evidence" phải trích NGUYÊN VĂN một câu có trong chương.
- Các trường mô tả thị giác ("appearance", "value", "description") viết bằng tiếng Anh, ngắn gọn, dùng cho model vẽ ảnh.
- Nhân vật / bối cảnh đã có trong danh sách: dùng đúng tên chính, KHÔNG tạo mới. Gặp tên gọi khác của người đã biết thì vẫn dùng tên chính.
- Chỉ thêm nhân vật mới khi họ xuất hiện trực tiếp trong cảnh, không thêm người chỉ được nhắc tên.

Sự kiện nhân vật ("events"):
- "field" thuộc: outfit, hair, injury, mark, age, body, face, accessory, other.
- "injury": vết thương tạm thời (băng bó, bầm tím). "mark": dấu vết VĨNH VIỄN trên cơ thể (sẹo, hình xăm), ghi rõ vị trí, ví dụ "long scar on right cheek".
- "value" rỗng "" nghĩa là trạng thái đó kết thúc (tháo băng, cởi áo giáp).
- "permanent": true cho thay đổi vĩnh viễn. "lasts_chapters": số chương trạng thái tạm thời kéo dài nếu truyện cho biết, không biết thì null.

Bối cảnh:
- "locations": các nơi xuất hiện trong chương (chưa có trong danh sách). KHÔNG tách bối cảnh theo ngày/đêm/mưa.
- "location_events": thay đổi lâu dài của bối cảnh, "field" thuộc condition (bị phá hủy, cháy...), decor (trang trí lễ hội...), other.

"summary": tóm tắt chương bằng tiếng Việt, 2 đến 4 câu, giữ các sự kiện quan trọng."""

BEATS_SYSTEM = """Bạn chia một chương truyện thành các nhịp (beat) để minh họa. Mỗi beat là một khung hình.

Chỉ trả về MỘT đối tượng JSON hợp lệ theo schema, không thêm chữ nào khác.

Quy tắc:
- Đoạn văn đánh số [1]..[n]. Mỗi beat là một khoảng đoạn liên tiếp "start".."end".
- BẮT BUỘC: các beat nối tiếp nhau, phủ toàn bộ chương từ 1 đến n, không chồng lấn, không bỏ sót.
- Mỗi beat thường gồm 1 đến 4 đoạn; cắt khi đổi cảnh, đổi hành động chính hoặc đổi người nói.
- "location": đúng tên bối cảnh trong danh sách. "variant": ánh sáng/thời tiết của cảnh: default, dawn, day, dusk, night, rain, snow, fog...
- "cast": tối đa 3 nhân vật thực sự xuất hiện trong khung hình, dùng đúng tên chính. "pose", "expression" bằng tiếng Anh.
- "shot": wide (toàn cảnh, mở cảnh), medium, close (cận mặt, cảm xúc).
- "action", "mood": tiếng Anh, mô tả thị giác ngắn gọn để vẽ ảnh.
- "dialogue": lời thoại nguyên văn bằng ngôn ngữ gốc, kèm "character" là người nói, "kind" là speech, thought hoặc caption."""

DEDUPE_SYSTEM = """Bạn rà soát danh sách nhân vật và bối cảnh của một bộ truyện để tìm các mục BỊ TRÙNG (cùng một người / một nơi nhưng bị ghi thành hai mục do viết tắt, biệt danh, cách xưng hô, lỗi chính tả).

Chỉ trả về MỘT đối tượng JSON. "keep" là tên mục giữ lại (thường xuất hiện sớm hơn), "merge" là tên mục gộp vào. Chỉ liệt kê khi chắc chắn; người khác nhau có tên gần giống thì KHÔNG gộp. "reason" bằng tiếng Việt, ngắn."""


def _chars_block(chars: list[dict], describe) -> str:
    lines = []
    for c in chars:
        aliases = ", ".join(c.get("aliases") or [])
        s = describe(c.get("state") or {})
        lines.append(f"- {c['name']}" + (f" (còn gọi: {aliases})" if aliases else "")
                     + (f" | ngoại hình: {c['appearance']}" if c.get("appearance") else "")
                     + (f" | trạng thái hiện tại: {s}" if s else ""))
    return "\n".join(lines) or "(chưa có)"


def _locs_block(locs: list[dict]) -> str:
    out = []
    for l in locs:
        st = "; ".join(f"{k}: {v}" for k, v in (l.get("state") or {}).items())
        out.append(f"- {l['name']}: {l.get('description', '')}" + (f" | hiện trạng: {st}" if st else ""))
    return "\n".join(out) or "(chưa có)"


def extract_messages(project: dict, chapter: dict, chars: list[dict], locs: list[dict],
                     summaries: list[dict], describe, extra: str = "") -> list[dict]:
    prev = "\n".join(f"Chương {s['idx']}: {s['summary']}" for s in summaries if s.get("summary")) or "(chưa có)"
    example = {
        "summary": "...",
        "new_characters": [{"name": "Tên", "aliases": ["biệt danh"], "appearance": "young man, short black hair, thin", "role": "nhân vật chính"}],
        "events": [{"character": "Tên", "field": "mark", "value": "long scar on right cheek", "permanent": True,
                    "lasts_chapters": None, "evidence": "câu nguyên văn trong chương"}],
        "locations": [{"name": "Tên nơi", "aliases": [], "description": "misty mountain sect, stone stairs"}],
        "location_events": [{"location": "Tên nơi", "field": "condition", "value": "burned ruins", "permanent": True,
                             "lasts_chapters": None, "evidence": "câu nguyên văn"}],
    }
    user = (
        f"PHONG CÁCH TRUYỆN: {project.get('style_prompt') or '(không có)'}\n\n"
        f"NHÂN VẬT ĐÃ BIẾT (trạng thái tính đến hết chương trước):\n{_chars_block(chars, describe)}\n\n"
        f"BỐI CẢNH ĐÃ BIẾT:\n{_locs_block(locs)}\n\n"
        f"TÓM TẮT CÁC CHƯƠNG TRƯỚC:\n{prev}\n\n"
        f"CHƯƠNG {chapter['idx']}: {chapter.get('title', '')}\n\"\"\"\n{chapter['text']}\n\"\"\"\n\n"
        f"Ví dụ cấu trúc JSON:\n{json.dumps(example, ensure_ascii=False)}"
    )
    sys = EXTRACT_SYSTEM + (f"\n\nHướng dẫn thêm:\n{extra}" if extra else "")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def beats_messages(project: dict, chapter: dict, paras: list[str], chars: list[dict], locs: list[dict],
                   describe, extra: str = "") -> list[dict]:
    numbered = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(paras, 1))
    example = {"beats": [{"start": 1, "end": 2, "location": "Tên nơi", "variant": "dawn",
                          "cast": [{"character": "Tên", "pose": "climbing stairs", "expression": "determined"}],
                          "shot": "wide", "action": "a young man climbs stone stairs toward a temple gate",
                          "mood": "misty, calm", "dialogue": [{"character": "Tên", "text": "lời thoại", "kind": "speech"}]}]}
    user = (
        f"NHÂN VẬT (trạng thái tại chương này):\n{_chars_block(chars, describe)}\n\n"
        f"BỐI CẢNH:\n{_locs_block(locs)}\n\n"
        f"CHƯƠNG {chapter['idx']}: {chapter.get('title', '')} — có {len(paras)} đoạn, beat cuối phải kết thúc ở {len(paras)}.\n\n"
        f"{numbered}\n\nVí dụ cấu trúc JSON:\n{json.dumps(example, ensure_ascii=False)}"
    )
    sys = BEATS_SYSTEM + (f"\n\nHướng dẫn thêm:\n{extra}" if extra else "")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def dedupe_messages(chars: list[dict], locs: list[dict]) -> list[dict]:
    cl = "\n".join(f"- {c['name']} (còn gọi: {', '.join(c.get('aliases') or []) or '-'}; từ chương {c['first_chapter']}; "
                   f"{c.get('appearance') or ''}; {c.get('role') or ''})" for c in chars) or "(trống)"
    ll = "\n".join(f"- {l['name']} (từ chương {l['first_chapter']}; {l.get('description') or ''})" for l in locs) or "(trống)"
    example = {"characters": [{"keep": "Lâm An", "merge": "An ca", "reason": "An ca là cách gọi thân mật Lâm An"}],
               "locations": []}
    user = (f"NHÂN VẬT:\n{cl}\n\nBỐI CẢNH:\n{ll}\n\nVí dụ JSON:\n{json.dumps(example, ensure_ascii=False)}")
    return [{"role": "system", "content": DEDUPE_SYSTEM}, {"role": "user", "content": user}]
