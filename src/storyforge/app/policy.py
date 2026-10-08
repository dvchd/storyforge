"""Cong duyet va chinh sach duyet nhieu cap.

Duyet tu dong chi la mot nguoi duyet dac biet: moi muc deu di qua decide(),
ket qua la 'approved' hoac 'pending'. Engine chi di tiep sau khi muc duoc duyet.
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
    "state": "G2 Trạng thái nhân vật theo chương",
    "breakdown": "G3 Chia nhịp (beat)",
    "reference": "G4 Ảnh tham chiếu: mặt, trang phục, bối cảnh",
    "scene": "G5 Ảnh cảnh / ảnh khung truyện",
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

# Co canh bao do app tu kiem tra.
FLAG_LABELS = {
    "new_identity": "Nhân vật / bối cảnh mới",
    "unknown_character": "Tên không khớp danh sách nhân vật",
    "unknown_location": "Bối cảnh chưa biết",
    "evidence_missing": "Câu trích không có trong chương",
    "permanent": "Thay đổi vĩnh viễn",
    "conflict": "Mâu thuẫn với trạng thái hiện có",
    "redundant": "Trùng với trạng thái hiện có",
    "span_adjusted": "Khoảng đoạn văn đã bị chỉnh lại",
    "too_many_cast": "Quá nhiều nhân vật trong một khung",
    "no_location": "Chưa gắn bối cảnh",
    "audio_mismatch": "Thời lượng audio bất thường",
    "stale": "Đầu vào đã thay đổi",
    "regenerated": "Đã tạo lại",
}


class GatePolicy(BaseModel):
    mode: GateMode = GateMode.MANUAL
    batch: str = "item"                  # item | chapter
    sample_rate: float = 0.1
    always_review: list[str] = Field(default_factory=list)


_SAFE = ["conflict", "evidence_missing"]


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
            "identity": _g("auto_if_clean", always=_SAFE + ["unknown_character", "unknown_location"]),
            "state": _g("auto_if_clean", always=_SAFE),
            "breakdown": _g("auto_if_clean"),
            "reference": _g("auto_if_clean"),
            "scene": _g("auto"),
            "audio": _g("auto"),
            "publish": _g("auto"),
        },
    },
    "2": {
        "label": "2. Duyệt nền tảng (khuyên dùng)",
        "desc": "Bạn duyệt danh tính, ảnh tham chiếu và thay đổi vĩnh viễn. Còn lại tự chạy.",
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
        "desc": "Trạng thái và chia nhịp duyệt theo cả chương.",
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


def effective(project: dict, chapter: dict | None = None) -> dict[str, GatePolicy]:
    """Mac dinh theo cap -> de theo du an -> de theo chuong (cap va tung cong)."""
    pp = project_policy(project)
    level = str(pp.get("level", "2"))
    over_gates = dict(pp.get("gates") or {})
    if chapter is not None:
        cp = jl(chapter.get("policy_json"), {}) or {}
        if cp.get("level") not in (None, ""):
            level = str(cp["level"])
            over_gates = {}
        for g, v in (cp.get("gates") or {}).items():
            over_gates[g] = {**over_gates.get(g, {}), **v}
    base = copy.deepcopy(LEVELS.get(level, LEVELS["2"])["gates"])
    out: dict[str, GatePolicy] = {}
    for g in GATES:
        merged = {**base.get(g, _g("manual")), **{k: v for k, v in (over_gates.get(g) or {}).items() if v not in (None, "")}}
        out[g] = GatePolicy(**merged)
    return out


# Co mang tinh thong tin: khong tu chan duyet tu dong, tru khi nam trong always_review.
INFO_FLAGS = {"new_identity", "regenerated", "permanent"}


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
    pp = project_policy(project)
    level = str(pp.get("level"))
    if chapter is not None:
        cp = jl(chapter.get("policy_json"), {}) or {}
        level = str(cp.get("level") or level)
    return f"auto:L{level}:{gate}"
