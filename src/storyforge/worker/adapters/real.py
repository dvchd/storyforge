"""Adapter cho model that. Thu vien chi duoc import khi adapter duoc dung,
nen may khong cai thu vien do van chay duoc cac adapter khac.
"""
from __future__ import annotations

import asyncio
import gc
import re
from pathlib import Path

from storyforge.protocol import ClaimedJob, ImageGeneratePayload, RemoveBgPayload, TtsPayload

from .base import Adapter, AdapterResult, JobContext, probe_duration


# ======================================================================= diffusers
class DiffusersImage(Adapter):
    """Tao anh bang diffusers. Ho tro anh tham chieu voi pipeline nhan tham so image
    (vi du FLUX.2 klein: black-forest-labs/FLUX.2-klein-4B). Chay CUDA, MPS hoac CPU.
    """
    kind = "image.generate"
    type_name = "diffusers"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.pipe = None
        self.device = None

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
        self.pipe, self.device = pipe, dev
        self._loaded = True

    def unload(self) -> None:
        self.pipe = None
        self._loaded = False
        gc.collect()
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            if hasattr(torch, "mps") and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        import torch  # type: ignore
        from PIL import Image

        self.load()
        p = ImageGeneratePayload(**job.payload)
        kw: dict = {"prompt": p.prompt, "width": p.width, "height": p.height,
                    "generator": torch.Generator(device="cpu").manual_seed(p.seed)}
        steps = p.steps or self.cfg.get("default_steps")
        if steps:
            kw["num_inference_steps"] = int(steps)
        if self.cfg.get("guidance") is not None:
            kw["guidance_scale"] = float(self.cfg["guidance"])
        if p.negative_prompt and self.cfg.get("use_negative", False):
            kw["negative_prompt"] = p.negative_prompt
        refs = [Image.open(ctx.fetch_asset(r.asset_id, r.sha256)).convert("RGB") for r in p.refs[: int(self.cfg.get("max_refs", 4))]]
        if refs:
            kw["image"] = refs if len(refs) > 1 else refs[0]
        img = self.pipe(**kw).images[0]
        out = ctx.workdir / f"img_{job.id}.png"
        img.save(out)
        return AdapterResult(output={"files": [out.name], "seed": p.seed}, files=[out], model_id=self.model)


# ======================================================================= Edge TTS
class EdgeTTS(Adapter):
    """Microsoft Edge TTS (cong cu khong chinh thuc, can Internet, chu y dieu khoan su dung).
    Giong tieng Viet: vi-VN-HoaiMyNeural, vi-VN-NamMinhNeural.
    """
    kind = "tts.synthesize"
    type_name = "edge_tts"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        import edge_tts  # type: ignore

        p = TtsPayload(**job.payload)
        voice = p.voice or self.cfg.get("voice", "vi-VN-HoaiMyNeural")
        pct = int(round((p.rate - 1.0) * 100))
        rate = f"{pct:+d}%"
        out = ctx.workdir / f"tts_{job.id}.mp3"
        sentences: list[dict] = []

        async def go() -> None:
            try:
                com = edge_tts.Communicate(p.text, voice, rate=rate, boundary="SentenceBoundary")
            except TypeError:  # phien ban cu khong co tham so boundary
                com = edge_tts.Communicate(p.text, voice, rate=rate)
            with out.open("wb") as f:
                async for chunk in com.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] in ("SentenceBoundary", "WordBoundary"):
                        s = chunk["offset"] / 1e7
                        sentences.append({"start": s, "end": s + chunk["duration"] / 1e7, "text": chunk["text"]})

        asyncio.run(go())
        dur = probe_duration(out, str(self.cfg.get("ffprobe", "ffprobe"))) or (sentences[-1]["end"] if sentences else 0.0)
        return AdapterResult(output={"file": out.name, "duration": dur, "sentences": sentences}, files=[out],
                             model_id=f"edge:{voice}")


# ======================================================================= VieNeu
class VieNeuTTS(Adapter):
    """VieNeu-TTS (pip install vieneu). Chay CPU qua ONNX, khong can GPU.
    voice: ten giong dung san (vi du "Thiện Minh"), hoac de trong va dat ref_audio de bat chuoc giong.
    """
    kind = "tts.synthesize"
    type_name = "vieneu"

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
        sentences = _even_sentences(p.text, dur)
        return AdapterResult(output={"file": out.name, "duration": dur, "sentences": sentences}, files=[out],
                             model_id=f"vieneu:{voice or 'default'}")


def _even_sentences(text: str, dur: float) -> list[dict]:
    parts = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.replace("\n", " ")) if s.strip()]
    total = max(1, sum(len(s) for s in parts))
    out, t = [], 0.0
    for s in parts:
        d = dur * len(s) / total
        out.append({"start": round(t, 3), "end": round(t + d, 3), "text": s})
        t += d
    return out


# ======================================================================= rembg
class RembgRemoveBg(Adapter):
    kind = "image.remove_bg"
    type_name = "rembg"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        from PIL import Image
        from rembg import remove  # type: ignore

        p = RemoveBgPayload(**job.payload)
        src = ctx.fetch_asset(p.image.asset_id, p.image.sha256)
        out = ctx.workdir / f"nobg_{job.id}.png"
        remove(Image.open(src)).save(out)
        return AdapterResult(output={"files": [out.name]}, files=[out], model_id="rembg")


Path  # giu import cho type checker
