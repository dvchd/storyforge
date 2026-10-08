"""Hợp đồng duy nhất giữa app và worker.

App và worker chỉ giao tiếp qua các schema trong gói này. Mỗi payload mang
SCHEMA_VERSION; app từ chối worker khác phiên bản để tránh hỏng dữ liệu.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = 2


class JobKind(str, Enum):
    LLM_CHAT = "llm.chat"
    IMAGE_GENERATE = "image.generate"
    IMAGE_REMOVE_BG = "image.remove_bg"
    TTS_SYNTHESIZE = "tts.synthesize"


# Việc không cần AI, chạy ngay trong tiến trình app (ffmpeg, Pillow).
LOCAL_KINDS = ("local.render_video", "local.compose_comic")
AI_KINDS = tuple(k.value for k in JobKind)

# Thời gian thuê mặc định (giây) theo loại job. Worker gửi heartbeat khoảng lease/3.
DEFAULT_LEASE = {
    "llm.chat": 900,
    "image.generate": 1800,
    "image.remove_bg": 600,
    "tts.synthesize": 600,
    "local.render_video": 3600,
    "local.compose_comic": 1800,
}


# ---------------------------------------------------------------- payloads
class RefImage(BaseModel):
    asset_id: int
    role: str = ""          # vai trò, ví dụ "the face of Lam An"
    sha256: str = ""        # worker dùng để cache và kiểm tra toàn vẹn file


class LlmChatPayload(BaseModel):
    schema_version: int = SCHEMA_VERSION
    messages: list[dict[str, Any]]
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None
    # Thông tin phụ: adapter thật bỏ qua, adapter giả lập dùng để mô phỏng.
    meta: dict[str, Any] = Field(default_factory=dict)


class ImageGeneratePayload(BaseModel):
    schema_version: int = SCHEMA_VERSION
    prompt: str
    negative_prompt: str = ""
    refs: list[RefImage] = Field(default_factory=list)
    width: int = 1024
    height: int = 1024
    seed: int = 0
    steps: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class RemoveBgPayload(BaseModel):
    schema_version: int = SCHEMA_VERSION
    image: RefImage


class TtsPayload(BaseModel):
    schema_version: int = SCHEMA_VERSION
    text: str
    voice: str = ""
    rate: float = 1.0
    language: str = "vi"
    meta: dict[str, Any] = Field(default_factory=dict)


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    JobKind.LLM_CHAT.value: LlmChatPayload,
    JobKind.IMAGE_GENERATE.value: ImageGeneratePayload,
    JobKind.IMAGE_REMOVE_BG.value: RemoveBgPayload,
    JobKind.TTS_SYNTHESIZE.value: TtsPayload,
}


# ----------------------------------------------------------------- outputs
class LlmChatOutput(BaseModel):
    text: str
    usage: dict[str, Any] = Field(default_factory=dict)   # prompt_tokens, completion_tokens


class ImageOutput(BaseModel):
    files: list[str]
    seed: int | None = None


class Timing(BaseModel):
    start: float
    end: float
    text: str


class TtsOutput(BaseModel):
    file: str
    duration: float
    sentences: list[Timing] = Field(default_factory=list)
    words: list[Timing] = Field(default_factory=list)      # nếu engine TTS trả mốc từng từ


# ------------------------------------------------------------- worker API
class ClaimRequest(BaseModel):
    schema_version: int = SCHEMA_VERSION
    worker_id: str
    name: str = ""
    capabilities: list[str]
    loaded_models: list[str] = Field(default_factory=list)
    model_ids: list[str] = Field(default_factory=list)


class ClaimedJob(BaseModel):
    id: int
    kind: str
    payload: dict[str, Any]
    model_hint: str = ""
    attempts: int = 1
    lease_seconds: int = 600
    schema_version: int = SCHEMA_VERSION


class HeartbeatRequest(BaseModel):
    progress: float | None = None        # 0..1
    message: str = ""


class HeartbeatResponse(BaseModel):
    ok: bool = True
    cancel: bool = False


class CompleteRequest(BaseModel):
    output: dict[str, Any]
    model_id: str = ""
    elapsed: float = 0.0


class FailRequest(BaseModel):
    error: str
    retryable: bool = True
    cancelled: bool = False


__all__ = [n for n in dir() if not n.startswith("_")]
