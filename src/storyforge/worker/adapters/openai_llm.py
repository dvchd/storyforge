"""LLM qua API tuong thich OpenAI.

Dung duoc voi: mlx_lm.server, LM Studio, Ollama (/v1), llama.cpp server, vLLM,
OpenRouter, OpenAI... Chi can base_url va ten model.
"""
from __future__ import annotations

import httpx

from storyforge.protocol import ClaimedJob, LlmChatPayload

from .base import Adapter, AdapterResult, JobContext, RetryableError


class OpenAICompatLLM(Adapter):
    kind = "llm.chat"
    type_name = "openai"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.base_url = str(cfg.get("base_url", "http://localhost:8080/v1")).rstrip("/")
        self.api_key = str(cfg.get("api_key", "local"))
        # json_mode: schema | object | none. Mot so server khong ho tro json_schema.
        self.json_mode = str(cfg.get("json_mode", "object"))
        self.timeout = float(cfg.get("timeout", 900))
        self.extra = dict(cfg.get("extra_body", {}))
        self._loaded = True  # server LLM tu quan ly model

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = LlmChatPayload(**job.payload)
        body: dict = {"model": self.model, "messages": p.messages, "temperature": p.temperature, **self.extra}
        if p.max_tokens:
            body["max_tokens"] = p.max_tokens
        if p.json_schema and self.json_mode == "schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": p.schema_name or "Output", "schema": p.json_schema}}
        elif p.json_schema and self.json_mode == "object":
            body["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as e:
            raise RetryableError(f"Không kết nối được LLM: {e}") from e
        if r.status_code == 400 and "response_format" in body:
            # server khong ho tro response_format: thu lai khong co
            body.pop("response_format")
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"})
        if r.status_code >= 500 or r.status_code == 429:
            raise RetryableError(f"LLM {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"].get("content") or ""
        return AdapterResult(output={"text": text, "usage": data.get("usage", {})},
                             model_id=data.get("model") or self.model)
