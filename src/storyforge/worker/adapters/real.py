"""Adapter cho model thật. Thư viện chỉ được import khi adapter được dùng."""
from __future__ import annotations

import asyncio
import gc
import re

from storyforge.protocol import ClaimedJob, ImageGeneratePayload, RemoveBgPayload, TtsPayload

from .base import Adapter, AdapterResult, Cancelled, JobContext, probe_duration


class DiffusersImage(Adapter):
    """diffusers; hỗ trợ ảnh tham chiếu với pipeline nhận tham số image (ví dụ FLUX.2 klein). CUDA, MPS hoặc CPU."""
    kind = "image.generate"
    type_name = "diffusers"
    heavy = True

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.pipe = None

    def load(self) -> None:
        if self.pipe is not None:
            return
        import torch  # type: ignore
        from diffusers import DiffusionPipeline  # type: ignore

        dev = self.cfg.get("device") or ("cuda" if torch.cuda.is_available()
                                          else "mps" if torch.backends.mps.is_available() else "cpu")
        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[self.cfg.get("dtype", "bf16")]
        pipe = DiffusionPipeline.from_pretrained(self.cfg.get("repo", self.model), torch_dtype=dtype)
        if self.cfg.get("cpu_offload") and dev == "cuda":
            pipe.enable_model_cpu_offload()
        else:
            pipe = pipe.to(dev)
        self.pipe = pipe
        self._loaded = True

    def unload(self) -> None:
        self.pipe = None
        self._loaded = False
        gc.collect()
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        import torch  # type: ignore
        from PIL import Image

        self.load()
        p = ImageGeneratePayload(**job.payload)
        steps = int(p.steps or self.cfg.get("default_steps") or 28)
        kw: dict = {"prompt": p.prompt, "width": p.width, "height": p.height, "num_inference_steps": steps,
                    "generator": torch.Generator(device="cpu").manual_seed(p.seed)}
        if self.cfg.get("guidance") is not None:
            kw["guidance_scale"] = float(self.cfg["guidance"])
        if p.negative_prompt and self.cfg.get("use_negative", False):
            kw["negative_prompt"] = p.negative_prompt
        refs = [Image.open(ctx.fetch_asset(r.asset_id, r.sha256)).convert("RGB")
                for r in p.refs[: int(self.cfg.get("max_refs", 4))]]
        if refs:
            kw["image"] = refs if len(refs) > 1 else refs[0]

        def on_step(pipe, i, t, cb_kwargs):  # noqa: ANN001
            ctx.report((i + 1) / steps, f"bước {i + 1}/{steps}")
            if ctx.is_cancelled():
                raise Cancelled("Job đã bị hủy")
            return cb_kwargs

        try:
            img = self.pipe(**kw, callback_on_step_end=on_step).images[0]
        except TypeError:
            img = self.pipe(**kw).images[0]
        out = ctx.workdir / f"img_{job.id}.png"
        img.save(out)
        return AdapterResult(output={"files": [out.name], "seed": p.seed}, files=[out], model_id=self.model)


class EdgeTTS(Adapter):
    """Microsoft Edge TTS (không chính thức, cần Internet). Giọng: vi-VN-HoaiMyNeural, vi-VN-NamMinhNeural."""
    kind = "tts.synthesize"
    type_name = "edge_tts"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        import edge_tts  # type: ignore

        p = TtsPayload(**job.payload)
        voice = p.voice or self.cfg.get("voice", "vi-VN-HoaiMyNeural")
        rate = f"{int(round((p.rate - 1.0) * 100)):+d}%"
        out = ctx.workdir / f"tts_{job.id}.mp3"
        words: list[dict] = []
        sentences: list[dict] = []

        async def go() -> None:
            try:
                com = edge_tts.Communicate(p.text, voice, rate=rate, boundary="WordBoundary")
            except TypeError:
                com = edge_tts.Communicate(p.text, voice, rate=rate)
            with out.open("wb") as f:
                async for chunk in com.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                        s = chunk["offset"] / 1e7
                        item = {"start": s, "end": s + chunk["duration"] / 1e7, "text": chunk["text"]}
                        (words if chunk["type"] == "WordBoundary" else sentences).append(item)

        asyncio.run(go())
        dur = probe_duration(out, str(self.cfg.get("ffprobe", "ffprobe"))) or (words[-1]["end"] if words else 0.0)
        return AdapterResult(output={"file": out.name, "duration": dur, "words": words, "sentences": sentences},
                             files=[out], model_id=f"edge:{voice}")


class VieNeuTTS(Adapter):
    """VieNeu-TTS (pip install vieneu), chạy CPU qua ONNX. voice: giọng dựng sẵn (ví dụ "Thiện Minh")."""
    kind = "tts.synthesize"
    type_name = "vieneu"
    heavy = True

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.tts = None

    def load(self) -> None:
        if self.tts is None:
            from vieneu import Vieneu  # type: ignore

            self.tts = Vieneu(**dict(self.cfg.get("init", {})))
        self._loaded = True

    def unload(self) -> None:
        self.tts = None
        self._loaded = False
        gc.collect()

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        self.load()
        p = TtsPayload(**job.payload)
        voice = p.voice or self.cfg.get("voice") or None
        kw = dict(self.cfg.get("infer", {}))
        if voice:
            kw["voice"] = voice
        if self.cfg.get("ref_audio"):
            kw["ref_audio"] = self.cfg["ref_audio"]
        audio = self.tts.infer(p.text, **kw)
        out = ctx.workdir / f"tts_{job.id}.wav"
        self.tts.save(audio, str(out))
        dur = probe_duration(out) or 0.0
        parts = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", p.text.replace("\n", " ")) if s.strip()]
        total = max(1, sum(len(s.split()) for s in parts))
        sentences, t = [], 0.0
        for s in parts:
            d = dur * len(s.split()) / total
            sentences.append({"start": round(t, 3), "end": round(t + d, 3), "text": s})
            t += d
        return AdapterResult(output={"file": out.name, "duration": dur, "sentences": sentences}, files=[out],
                             model_id=f"vieneu:{voice or 'default'}")


class RembgRemoveBg(Adapter):
    kind = "image.remove_bg"
    type_name = "rembg"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        from PIL import Image
        from rembg import remove  # type: ignore

        p = RemoveBgPayload(**job.payload)
        out = ctx.workdir / f"nobg_{job.id}.png"
        remove(Image.open(ctx.fetch_asset(p.image.asset_id, p.image.sha256))).save(out)
        return AdapterResult(output={"files": [out.name]}, files=[out], model_id="rembg")
