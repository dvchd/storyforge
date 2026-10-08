"""Cổng duyệt và chính sách duyệt nhiều cấp.

Duyệt tự động chỉ là một người duyệt đặc biệt: mọi mục đều đi qua decide(),
kết quả là 'approved' hoặc 'pending'. Engine chỉ đi tiếp sau khi mục được duyệt.
"""
from __future__ import annotations

import copy
import random
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .db import jl


class Gate(str, Enum):
    IDENTITY = "identity"
    STATE = "state"
    BREAKDOWN = "breakdown"
    REFERENCE = "reference"
    SCENE = "scene"
    AUDIO = "audio"
    PUBLISH = "publish"


GATES = [g.value for g in Gate]

GATE_LABELS = {
    "identity": "G1 Danh tính: nhân vật, bối cảnh mới",
    "state": "G2 Trạng thái nhân vật / bối cảnh theo chương",
    "breakdown": "G3 Chia nhịp",
    "reference": "G4 Ảnh tham chiếu: mặt, toàn thân, bối cảnh",
    "scene": "G5 Ảnh cảnh (dùng chung video và truyện tranh)",
    "audio": "G6 Giọng đọc",
    "publish": "G7 Dựng và xuất bản",
}


class GateMode(str, Enum):
    AUTO = "auto"
    AUTO_IF_CLEAN = "auto_if_clean"
    SAMPLE = "sample"
    MANUAL = "manual"


MODE_LABELS = {
    "auto": "Tự động",
    "auto_if_clean": "Tự động nếu không có cảnh báo",
    "sample": "Tự động, rút mẫu cho người xem",
    "manual": "Luôn chờ người duyệt",
}

FLAG_LABELS = {
    "new_identity": "Nhân vật / bối cảnh mới",
    "possible_duplicate": "Có thể trùng với mục đã có",
    "alias_collision": "Tên gọi khác trùng người khác",
    "unknown_character": "Tên không khớp danh sách nhân vật",
    "unknown_location": "Bối cảnh chưa biết",
    "evidence_missing": "Câu trích không có trong chương",
    "permanent": "Thay đổi vĩnh viễn",
    "conflict": "Mâu thuẫn với trạng thái hiện có",
    "age_regression": "Tuổi giảm so với trước",
    "redundant": "Trùng với trạng thái hiện có",
    "non_english_prompt": "Mô tả cho model ảnh không phải tiếng Anh",
    "span_adjusted": "Khoảng đoạn văn đã bị chỉnh lại",
    "too_many_cast": "Quá nhiều nhân vật trong một khung",
    "no_location": "Chưa gắn bối cảnh",
    "audio_mismatch": "Tốc độ đọc bất thường",
    "text_overflow": "Lời thoại tràn khung",
    "regenerated": "Đã tạo lại",
}
INFO_FLAGS = {"new_identity", "regenerated", "permanent", "redundant"}


class GatePolicy(BaseModel):
    mode: GateMode = GateMode.MANUAL
    batch: str = "item"
    sample_rate: float = 0.1
    always_review: list[str] = Field(default_factory=list)


_SAFE = ["conflict", "evidence_missing", "age_regression"]


def _g(mode: str, batch: str = "item", sample: float = 0.1, always: list[str] | None = None) -> dict:
    return {"mode": mode, "batch": batch, "sample_rate": sample, "always_review": list(always or [])}


LEVELS: dict[str, dict[str, Any]] = {
    "0": {
        "label": "0. Tự động hoàn toàn",
        "desc": "Không dừng lại ở đâu, trừ điểm kiểm tra định kỳ.",
        "gates": {g: _g("auto") for g in GATES},
    },
    "1": {
        "label": "1. Tự động có ngoại lệ",
        "desc": "Chỉ dừng khi app phát hiện cảnh báo.",
        "gates": {
            "identity": _g("auto_if_clean", always=["possible_duplicate", "alias_collision", "unknown_character"]),
            "state": _g("auto_if_clean", always=_SAFE),
            "breakdown": _g("auto_if_clean"),
            "reference": _g("auto_if_clean"),
            "scene": _g("auto"),
            "audio": _g("auto_if_clean"),
            "publish": _g("auto"),
        },
    },
    "2": {
        "label": "2. Duyệt nền tảng (khuyên dùng)",
        "desc": "Bạn duyệt danh tính, ảnh tham chiếu và thay đổi vĩnh viễn. Còn lại tự chạy, rút mẫu 10%.",
        "gates": {
            "identity": _g("manual"),
            "state": _g("auto_if_clean", always=_SAFE + ["permanent"]),
            "breakdown": _g("auto_if_clean"),
            "reference": _g("manual"),
            "scene": _g("sample", sample=0.1),
            "audio": _g("sample", sample=0.1),
            "publish": _g("manual"),
        },
    },
    "3": {
        "label": "3. Duyệt từng chương",
        "desc": "Trạng thái và chia nhịp duyệt gộp theo cả chương.",
        "gates": {
            "identity": _g("manual"),
            "state": _g("manual", batch="chapter"),
            "breakdown": _g("manual", batch="chapter"),
            "reference": _g("manual"),
            "scene": _g("auto_if_clean"),
            "audio": _g("auto_if_clean"),
            "publish": _g("manual"),
        },
    },
    "4": {
        "label": "4. Duyệt chặt",
        "desc": "Mọi bước đều chờ người duyệt.",
        "gates": {g: _g("manual") for g in GATES},
    },
}

DEFAULT_POLICY = {"level": "2", "gates": {}, "checkpoint_every": 10}


def project_policy(project: dict) -> dict:
    p = copy.deepcopy(DEFAULT_POLICY)
    p.update(jl(project.get("policy_json"), {}) or {})
    return p


def chapter_policy(chapter: dict | None) -> dict:
    return (jl(chapter.get("policy_json"), {}) or {}) if chapter else {}


def effective(project: dict, chapter: dict | None = None) -> dict[str, GatePolicy]:
    """Cấp (của chương nếu có, không thì của dự án) -> ghi đè cổng của dự án -> ghi đè cổng của chương."""
    pp = project_policy(project)
    cp = chapter_policy(chapter)
    level = str(cp.get("level") or pp.get("level", "2"))
    gates = copy.deepcopy(LEVELS.get(level, LEVELS["2"])["gates"])
    for layer in (pp.get("gates") or {}, cp.get("gates") or {}):
        for g, v in layer.items():
            if g in gates and v:
                gates[g].update({k: val for k, val in v.items() if val not in (None, "")})
    return {g: GatePolicy(**gates.get(g, _g("manual"))) for g in GATES}


def decide(gp: GatePolicy, flags: set[str] | list[str], rng: random.Random) -> str:
    flags = set(flags or [])
    if flags & set(gp.always_review):
        return "pending"
    warn = flags - INFO_FLAGS
    if gp.mode == GateMode.AUTO:
        return "approved"
    if gp.mode == GateMode.AUTO_IF_CLEAN:
        return "pending" if warn else "approved"
    if gp.mode == GateMode.SAMPLE:
        return "pending" if warn or rng.random() < gp.sample_rate else "approved"
    return "pending"


def policy_name(project: dict, chapter: dict | None, gate: str) -> str:
    level = str(chapter_policy(chapter).get("level") or project_policy(project).get("level"))
    return f"auto:L{level}:{gate}"
