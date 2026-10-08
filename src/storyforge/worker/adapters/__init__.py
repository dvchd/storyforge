"""Dang ky adapter theo (kind, type)."""
from __future__ import annotations

from .base import Adapter, AdapterResult, JobContext, RetryableError
from .command import CommandImage, CommandRemoveBg, CommandTTS
from .mock import MockImage, MockLLM, MockRemoveBg, MockTTS
from .openai_llm import OpenAICompatLLM
from .real import DiffusersImage, EdgeTTS, RembgRemoveBg, VieNeuTTS

REGISTRY: dict[tuple[str, str], type[Adapter]] = {
    ("llm.chat", "mock"): MockLLM,
    ("llm.chat", "openai"): OpenAICompatLLM,
    ("image.generate", "mock"): MockImage,
    ("image.generate", "command"): CommandImage,
    ("image.generate", "mflux"): CommandImage,       # mflux chay qua dong lenh
    ("image.generate", "diffusers"): DiffusersImage,
    ("tts.synthesize", "mock"): MockTTS,
    ("tts.synthesize", "edge_tts"): EdgeTTS,
    ("tts.synthesize", "vieneu"): VieNeuTTS,
    ("tts.synthesize", "command"): CommandTTS,
    ("image.remove_bg", "mock"): MockRemoveBg,
    ("image.remove_bg", "rembg"): RembgRemoveBg,
    ("image.remove_bg", "command"): CommandRemoveBg,
}


def build(cfg: dict) -> Adapter:
    key = (str(cfg["kind"]), str(cfg["type"]))
    if key not in REGISTRY:
        raise ValueError(f"Không có adapter {key}. Có: {sorted(REGISTRY)}")
    return REGISTRY[key](cfg)


__all__ = ["Adapter", "AdapterResult", "JobContext", "RetryableError", "REGISTRY", "build"]
