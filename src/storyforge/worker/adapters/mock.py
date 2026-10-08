"""Adapter gia lap: chay toan bo quy trinh khong can model AI.

Dung de thu giao dien, cong duyet va luong du lieu truoc khi gan model that.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import textwrap
import time
import wave
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw

from storyforge.protocol import ClaimedJob, ImageGeneratePayload, LlmChatPayload, RemoveBgPayload, TtsPayload

from .base import Adapter, AdapterResult, JobContext

LOC_PREPS = ("tại", "ở", "đến", "tới", "về", "vào", "rời", "trong", "ra")
STOP = {"Chương", "Hắn", "Nàng", "Cô", "Anh", "Ông", "Bà", "Sáng", "Chiều", "Tối", "Đêm", "Khi", "Sau", "Trước",
        "Một", "Hai", "Ba", "Cả", "Ngay", "Rồi", "Nhưng", "Và", "Lúc", "Trên", "Dưới", "Giữa", "Không", "Cậu", "Lão", "Tiểu"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+|[.!?…\n]", text)


def _cap(w: str) -> bool:
    return w[:1].isupper() and w.isalpha()


def _candidates(text: str) -> tuple[Counter, Counter]:
    """Tra ve (nhan vat, boi canh) theo tan suat cum tu viet hoa."""
    toks = _tokens(text)
    chars, locs = Counter(), Counter()
    i = 0
    while i < len(toks):
        if _cap(toks[i]):
            j = i
            while j < len(toks) and _cap(toks[j]) and j - i < 4:
                j += 1
            group = toks[i:j]
            while group and group[0] in STOP:
                group = group[1:]
            if 2 <= len(group) <= 4:
                name = " ".join(group)
                prev = toks[i - 1].lower() if i > 0 else ""
                (locs if prev in LOC_PREPS else chars)[name] += 1
            i = max(j, i + 1)
        else:
            i += 1
    return chars, locs


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.replace("\n", " ")) if s.strip()]


class MockLLM(Adapter):
    kind = "llm.chat"
    type_name = "mock-llm"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = LlmChatPayload(**job.payload)
        task = p.meta.get("task")
        time.sleep(float(self.cfg.get("delay", 0)))
        if task == "extract":
            out = self._extract(p.meta)
        elif task == "beats":
            out = self._beats(p.meta)
        else:
            out = {"text": "ok"}
        return AdapterResult(output={"text": json.dumps(out, ensure_ascii=False)}, model_id=self.model)

    def _known(self, meta: dict) -> dict[str, str]:
        idx = {}
        for c in meta.get("known_characters", []):
            for n in [c["name"], *c.get("aliases", [])]:
                idx[n.lower()] = c["name"]
        return idx

    def _extract(self, meta: dict) -> dict:
        text = meta.get("chapter_text", "")
        known = self._known(meta)
        known_locs = {l.lower() for l in meta.get("known_locations", [])}
        chars, locs = _candidates(text)
        new_chars = [n for n, k in chars.items() if k >= 2 and n.lower() not in known and n not in locs]
        new_locs = [n for n, k in locs.items() if n.lower() not in known_locs and n not in chars]
        all_names = list(known.values()) + new_chars
        events = []
        for s in _sentences(text):
            who = next((n for n in all_names if n.lower() in s.lower()), None)
            if not who:
                continue
            low = s.lower()
            m = re.search(r"(khoác|mặc|thay)\s+([^,.;!?]+)", s, re.I)
            if m:
                events.append({"character": who, "field": "outfit", "value": m.group(2).strip()[:60],
                               "permanent": False, "lasts_chapters": None, "evidence": s})
            if "sẹo" in low:
                events.append({"character": who, "field": "injury", "value": "scar on the face",
                               "permanent": True, "lasts_chapters": None, "evidence": s})
            elif "bị thương" in low or "băng bó" in low:
                events.append({"character": who, "field": "injury", "value": "bandaged wound",
                               "permanent": False, "lasts_chapters": 2, "evidence": s})
            if "tóc" in low and re.search(r"cắt|nhuộm|búi", low):
                events.append({"character": who, "field": "hair", "value": "new hairstyle",
                               "permanent": False, "lasts_chapters": None, "evidence": s})
        sents = _sentences(text)
        return {
            "summary": " ".join(sents[:2])[:400],
            "new_characters": [{"name": n, "aliases": [], "appearance": f"distinct look for {n}", "role": ""} for n in new_chars],
            "events": events,
            "locations": [{"name": n, "description": f"scenic view of {n}", "variant": "default"} for n in new_locs],
        }

    def _beats(self, meta: dict) -> dict:
        paras = meta.get("paragraphs", [])
        known = self._known(meta)
        locs = [l["name"] for l in meta.get("known_locations", [])]
        per = int(self.cfg.get("paras_per_beat", 2))
        beats, cur_loc = [], locs[0] if locs else ""
        shots = ["wide", "medium", "close"]
        for k, s in enumerate(range(0, len(paras), per)):
            chunk = " ".join(paras[s:s + per])
            low = chunk.lower()
            cast = []
            for alias, name in known.items():
                if alias in low and name not in [c["character"] for c in cast]:
                    cast.append({"character": name, "pose": "standing", "expression": "focused"})
            for l in locs:
                if l.lower() in low:
                    cur_loc = l
            quotes = re.findall(r"[“\"]([^”\"]+)[”\"]", chunk)
            dialogue = [{"character": cast[0]["character"] if cast else "", "text": q, "kind": "speech"} for q in quotes[:2]]
            beats.append({"start": s + 1, "end": min(s + per, len(paras)), "location": cur_loc, "variant": "default",
                          "cast": cast[:3], "shot": shots[k % 3], "action": "characters in the scene",
                          "mood": "cinematic", "dialogue": dialogue})
        return {"beats": beats}


class MockImage(Adapter):
    kind = "image.generate"
    type_name = "mock-image"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = ImageGeneratePayload(**job.payload)
        time.sleep(float(self.cfg.get("delay", 0)))
        w, h = max(64, p.width // 2), max(64, p.height // 2)    # anh nho cho nhanh
        hsh = hashlib.sha1(f"{p.prompt}{p.seed}".encode()).digest()
        bg = (80 + hsh[0] % 120, 80 + hsh[1] % 120, 90 + hsh[2] % 120)
        img = Image.new("RGB", (w, h), bg)
        d = ImageDraw.Draw(img)
        for i, ref in enumerate(p.refs[:4]):
            try:
                path = ctx.fetch_asset(ref.asset_id, ref.sha256)
                th = Image.open(path).convert("RGB")
                th.thumbnail((w // 4, h // 3))
                img.paste(th, (8 + i * (w // 4 + 4), h - th.height - 8))
            except Exception:  # noqa: BLE001
                pass
        y = 8
        for line in textwrap.wrap(p.prompt, width=max(20, w // 7))[:14]:
            d.text((8, y), line, fill=(255, 255, 255))
            y += 13
        d.text((w - 70, 8), f"seed {p.seed % 10000}", fill=(255, 255, 0))
        out = ctx.workdir / f"img_{job.id}.png"
        img.save(out, "PNG")
        return AdapterResult(output={"files": [out.name], "seed": p.seed}, files=[out], model_id=self.model)


class MockTTS(Adapter):
    kind = "tts.synthesize"
    type_name = "mock-tts"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = TtsPayload(**job.payload)
        cps = float(self.cfg.get("chars_per_second", 15)) * max(0.25, p.rate)
        dur = max(1.0, len(p.text) / cps)
        rate = 16000
        out = ctx.workdir / f"tts_{job.id}.wav"
        with wave.open(str(out), "wb") as wv:
            wv.setnchannels(1)
            wv.setsampwidth(2)
            wv.setframerate(rate)
            wv.writeframes(struct.pack("<h", 0) * int(dur * rate))
        sents, t, total = [], 0.0, max(1, len(p.text))
        for s in _sentences(p.text):
            d = dur * len(s) / total
            sents.append({"start": round(t, 3), "end": round(t + d, 3), "text": s})
            t += d
        return AdapterResult(output={"file": out.name, "duration": dur, "sentences": sents}, files=[out],
                             model_id=self.model)


class MockRemoveBg(Adapter):
    kind = "image.remove_bg"
    type_name = "mock-rembg"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = RemoveBgPayload(**job.payload)
        src = ctx.fetch_asset(p.image.asset_id, p.image.sha256)
        out = ctx.workdir / f"nobg_{job.id}.png"
        shutil.copyfile(src, out)
        return AdapterResult(output={"files": [out.name]}, files=[out], model_id=self.model)
