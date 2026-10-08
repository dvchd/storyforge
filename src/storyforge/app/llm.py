"""Schema dau ra LLM va cach dung prompt cho tung tac vu.

App tu dung prompt va tu kiem tra ket qua. Worker chi chay model.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from . import state as st

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

    @field_validator("field")
    @classmethod
    def _f(cls, v: str) -> str:
        return st.normalize_field(v)


class NewLocation(BaseModel):
    name: str
    description: str = ""
    variant: str = "default"


class ChapterExtraction(BaseModel):
    summary: str = ""
    new_characters: list[NewCharacter] = Field(default_factory=list)
    events: list[ProposedEvent] = Field(default_factory=list)
    locations: list[NewLocation] = Field(default_factory=list)


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
        return "medium"


class BeatsOut(BaseModel):
    beats: list[BeatOut]


# ------------------------------------------------------------ helpers
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    text = (text or "").strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    # bo phan suy nghi <think>...</think> cua mot so model
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
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


# ------------------------------------------------------------ prompts
EXTRACT_SYSTEM = """Bạn là trợ lý biên tập truyện. Nhiệm vụ: theo dõi nhân vật, bối cảnh và thay đổi ngoại hình của nhân vật qua từng chương để phục vụ vẽ minh họa nhất quán.

Chỉ trả về MỘT đối tượng JSON hợp lệ theo schema, không thêm chữ nào khác.

Quy tắc:
- Chỉ ghi thay đổi được nói rõ trong chương. Không suy đoán, không bịa.
- "evidence" phải trích NGUYÊN VĂN một câu có trong chương làm bằng chứng.
- "appearance", "value", "description" viết bằng tiếng Anh, ngắn gọn, mô tả thị giác, dùng cho model vẽ ảnh.
- "field" chỉ thuộc: outfit, hair, injury, age, body, face, accessory, other.
- "value" rỗng "" nghĩa là trạng thái đó kết thúc (ví dụ tháo băng, cởi áo giáp).
- "permanent": true cho thay đổi vĩnh viễn (sẹo, mất tay, đổi màu tóc lâu dài, già đi).
- "lasts_chapters": số chương một trạng thái tạm thời kéo dài nếu truyện cho biết, không biết thì null.
- Nhân vật đã có trong danh sách thì dùng đúng tên chính, không tạo trùng. Tên gọi khác thì thêm vào "aliases" khi tạo mới.
- Chỉ thêm nhân vật mới khi họ xuất hiện trực tiếp trong cảnh. Không thêm người chỉ được nhắc tên.
- "locations": các bối cảnh xuất hiện trong chương. "variant" là biến thể như "night", "rain", "ruined", mặc định "default".
- "summary": tóm tắt chương bằng tiếng Việt, 2 đến 4 câu, giữ các sự kiện quan trọng."""

BEATS_SYSTEM = """Bạn chia một chương truyện thành các nhịp (beat) để minh họa. Mỗi beat tương ứng một khung hình.

Chỉ trả về MỘT đối tượng JSON hợp lệ theo schema, không thêm chữ nào khác.

Quy tắc:
- Đoạn văn được đánh số [1]..[n]. Mỗi beat là một khoảng đoạn liên tiếp "start".."end".
- Các beat nối tiếp nhau, phủ toàn bộ chương từ 1 đến n, không chồng lấn, không bỏ sót.
- Mỗi beat thường gồm 1 đến 4 đoạn, cắt khi đổi cảnh, đổi hành động chính hoặc đổi người nói.
- "location": dùng đúng tên bối cảnh trong danh sách. "variant" như danh sách hoặc "default".
- "cast": tối đa 3 nhân vật thực sự xuất hiện trong khung hình, dùng đúng tên chính. "pose" và "expression" bằng tiếng Anh.
- "shot": wide, medium hoặc close.
- "action" và "mood": tiếng Anh, mô tả thị giác ngắn gọn để vẽ ảnh, không nhắc tên riêng khó hiểu.
- "dialogue": lời thoại nguyên văn bằng ngôn ngữ gốc của truyện, kèm "character" là người nói. "kind" là speech, thought hoặc caption."""


def _chars_block(chars: list[dict]) -> str:
    lines = []
    for c in chars:
        aliases = ", ".join(c.get("aliases") or [])
        s = st.describe(c.get("state") or {})
        lines.append(f"- {c['name']}" + (f" (còn gọi: {aliases})" if aliases else "")
                     + (f" | ngoại hình: {c['appearance']}" if c.get("appearance") else "")
                     + (f" | trạng thái hiện tại: {s}" if s else ""))
    return "\n".join(lines) or "(chưa có)"


def _locs_block(locs: list[dict]) -> str:
    return "\n".join(f"- {l['name']} [{l['variant']}]: {l.get('description', '')}" for l in locs) or "(chưa có)"


def extract_messages(project: dict, chapter: dict, chars: list[dict], locs: list[dict],
                     summaries: list[dict], extra: str = "") -> list[dict]:
    prev = "\n".join(f"Chương {s['idx']}: {s['summary']}" for s in summaries if s.get("summary")) or "(chưa có)"
    example = {
        "summary": "...",
        "new_characters": [{"name": "Tên", "aliases": ["biệt danh"], "appearance": "young man, short black hair, thin", "role": "nhân vật chính"}],
        "events": [{"character": "Tên", "field": "outfit", "value": "grey monk robe", "permanent": False,
                    "lasts_chapters": None, "evidence": "câu nguyên văn trong chương"}],
        "locations": [{"name": "Tên nơi", "description": "misty mountain sect, stone stairs", "variant": "default"}],
    }
    user = (
        f"PHONG CÁCH TRUYỆN: {project.get('style_prompt') or '(không có)'}\n\n"
        f"NHÂN VẬT ĐÃ BIẾT (trạng thái tính đến hết chương trước):\n{_chars_block(chars)}\n\n"
        f"BỐI CẢNH ĐÃ BIẾT:\n{_locs_block(locs)}\n\n"
        f"TÓM TẮT CÁC CHƯƠNG TRƯỚC:\n{prev}\n\n"
        f"CHƯƠNG {chapter['idx']}: {chapter.get('title', '')}\n\"\"\"\n{chapter['text']}\n\"\"\"\n\n"
        f"Ví dụ cấu trúc JSON:\n{json.dumps(example, ensure_ascii=False)}"
    )
    sys = EXTRACT_SYSTEM + (f"\n\nHướng dẫn thêm cho model này:\n{extra}" if extra else "")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def beats_messages(project: dict, chapter: dict, paras: list[str], chars: list[dict], locs: list[dict],
                   extra: str = "") -> list[dict]:
    numbered = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(paras, 1))
    example = {"beats": [{"start": 1, "end": 2, "location": "Tên nơi", "variant": "default",
                          "cast": [{"character": "Tên", "pose": "standing", "expression": "determined"}],
                          "shot": "wide", "action": "a young man climbs stone stairs toward a temple gate",
                          "mood": "misty dawn, calm", "dialogue": [{"character": "Tên", "text": "lời thoại", "kind": "speech"}]}]}
    user = (
        f"NHÂN VẬT (trạng thái tại chương này):\n{_chars_block(chars)}\n\n"
        f"BỐI CẢNH:\n{_locs_block(locs)}\n\n"
        f"CHƯƠNG {chapter['idx']}: {chapter.get('title', '')} ({len(paras)} đoạn)\n\n{numbered}\n\n"
        f"Ví dụ cấu trúc JSON:\n{json.dumps(example, ensure_ascii=False)}"
    )
    sys = BEATS_SYSTEM + (f"\n\nHướng dẫn thêm cho model này:\n{extra}" if extra else "")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]
