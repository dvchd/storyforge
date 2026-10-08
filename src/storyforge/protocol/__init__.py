"""Hop dong duy nhat giua app va worker.

App va worker chi giao tiep qua cac schema trong goi nay. Moi payload mang
SCHEMA_VERSION de nang cap mot ben khong lam hong ben kia.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class JobKind(str, Enum):
    LLM_CHAT = "llm.chat"
    IMAGE_GENERATE = "image.generate"
    IMAGE_REMOVE_BG = "image.remove_bg"
    TTS_SYNTHESIZE = "tts.synthesize"


# Cac viec khong can AI, chay ngay trong tien trinh app (ffmpeg, Pillow).
LOCAL_KINDS = ("local.render_video", "local.compose_comic")
AI_KINDS = tuple(k.value for k in JobKind)


# ---------------------------------------------------------------- payloads
class RefImage(BaseModel):
    asset_id: int
    role: str = ""          # mo ta vai tro, vi du "face of Lam An"
    sha256: str = ""        # worker dung de cache file da tai


class LlmChatPayload(BaseModel):
    schema_version: int = SCHEMA_VERSION
    messages: list[dict[str, Any]]
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None
    # Thong tin phu, adapter that bo qua; adapter mock dung de gia lap.
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
    usage: dict[str, Any] = Field(default_factory=dict)


class ImageOutput(BaseModel):
    files: list[str]                 # ten artifact da upload
    seed: int | None = None


class SentenceTiming(BaseModel):
    start: float
    end: float
    text: str


class TtsOutput(BaseModel):
    file: str
    duration: float
    sentences: list[SentenceTiming] = Field(default_factory=list)


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
    schema_version: int = SCHEMA_VERSION


class CompleteRequest(BaseModel):
    output: dict[str, Any]
    model_id: str = ""
    elapsed: float = 0.0


class FailRequest(BaseModel):
    error: str
    retryable: bool = True


class HeartbeatResponse(BaseModel):
    ok: bool = True
    cancel: bool = False


__all__ = [n for n in dir() if not n.startswith("_")]
