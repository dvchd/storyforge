"""Adapter giả lập: chạy toàn bộ quy trình không cần model AI.

Dùng để thử giao diện, cổng duyệt và luồng dữ liệu trước khi gắn model thật.
MockLLM đoán tên riêng theo cụm chữ viết hoa lặp lại (không gắn với truyện mẫu nào),
nhận diện vài mẫu câu về trang phục, vết thương, sẹo, phá hủy bối cảnh.
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

from PIL import Image, ImageDraw

from storyforge.protocol import ClaimedJob, ImageGeneratePayload, LlmChatPayload, RemoveBgPayload, TtsPayload

from .base import Adapter, AdapterResult, JobContext

LOC_PREPS = ("tại", "ở", "đến", "tới", "về", "vào", "rời", "trong", "ra", "khỏi", "quanh", "phố", "làng", "quán")
STOP = {"Chương", "Hắn", "Nàng", "Cô", "Anh", "Ông", "Bà", "Sáng", "Chiều", "Tối", "Đêm", "Khi", "Sau", "Trước",
        "Một", "Hai", "Ba", "Cả", "Ngay", "Rồi", "Nhưng", "Và", "Lúc", "Trên", "Dưới", "Giữa", "Không", "Cậu", "Lão",
        "Tiểu", "Đệ", "Mặt", "Ngươi", "Ta", "Tôi", "Em", "Chị", "Mẹ", "Bố", "Cha", "Con", "Đây", "Đó", "Vâng", "Ừ",
        "Phía", "Bên", "Thấy", "Tết", "Chào", "Hôm", "Ngoài", "Bức", "Cảm", "Mọi", "Những", "Các", "Nếu", "Vì", "Thế", "Có", "Đã", "Lần", "Coi", "Bám", "Hôm", "Ngày", "Năm"}


PLACE_WORDS = {"Tông", "Các", "Rừng", "Quán", "Nhà", "Phố", "Núi", "Điện", "Thành", "Trấn", "Sông", "Hồ", "Cung",
               "Viện", "Đường", "Làng", "Chợ", "Cầu", "Động", "Đảo", "Lâu", "Phủ", "Miếu", "Chùa", "Tháp"}


def _tokens(text: str) -> list[tuple[str, bool, bool]]:
    """(từ, đứng đầu câu, ngay sau dấu ngắt như dấu phẩy)."""
    out, start, brk = [], True, False
    for m in re.finditer(r"\w+|[.!?…:\n“\"”,;()]", text):
        w = m.group()
        if not re.match(r"\w", w):
            if w in ",;()":
                brk = True
            else:
                start = True
            continue
        out.append((w, start, brk))
        start = brk = False
    return out


def _cap(w: str) -> bool:
    return w[:1].isupper() and w.isalpha()


def _candidates(text: str) -> tuple[Counter, Counter]:
    """Đoán nhân vật và bối cảnh theo cụm chữ viết hoa."""
    toks = _tokens(text)
    chars, locs, starts = Counter(), Counter(), Counter()
    i = 0
    while i < len(toks):
        if _cap(toks[i][0]):
            j = i
            while j < len(toks) and _cap(toks[j][0]) and j - i < 5 and (j == i or not (toks[j][1] or toks[j][2])):
                j += 1
            group = [w for w, _, _ in toks[i:j]]
            while group and group[0] in STOP:
                group = group[1:]
            if group:
                name = " ".join(group)
                if len(group) == 1 and toks[i][1]:
                    starts[name] += 1            # một từ đầu câu: chỉ tính nếu lặp nhiều lần
                else:
                    prev = toks[i - 1][0].lower() if i > 0 else ""
                    (locs if prev in LOC_PREPS else chars)[name] += 1
            i = max(j, i + 1)
        else:
            i += 1
    for n, k in starts.items():
        if k + chars[n] >= 2:
            chars[n] += k
    for n in list(chars) + list(locs):
        is_loc = any(w in PLACE_WORDS for w in n.split()) or locs[n] > chars[n]
        (chars if is_loc else locs).pop(n, None)
        if is_loc and n not in locs:
            locs[n] = 1
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
        ctx.report(0.5, task or "")
        out = {"extract": self._extract, "beats": self._beats, "dedupe": self._dedupe}.get(task, lambda m: {"text": "ok"})(p.meta)
        text = json.dumps(out, ensure_ascii=False)
        return AdapterResult(output={"text": text, "usage": {"prompt_tokens": sum(len(str(m.get("content", ""))) // 4
                                                                                  for m in p.messages),
                                                             "completion_tokens": len(text) // 4}},
                             model_id=self.model)

    @staticmethod
    def _known(items: list[dict]) -> dict[str, str]:
        idx = {}
        for c in items:
            for n in [c["name"], *c.get("aliases", [])]:
                idx[n.lower()] = c["name"]
        return idx

    def _extract(self, meta: dict) -> dict:
        text = meta.get("chapter_text", "")
        known = self._known(meta.get("known_characters", []))
        known_locs = self._known(meta.get("known_locations", []))
        chars, locs = _candidates(text)
        new_locs = [n for n in locs if n.lower() not in known_locs and n.lower() not in known]
        new_chars = [n for n, k in chars.items() if k >= 2 and n.lower() not in known and n not in locs
                     and n.lower() not in known_locs and n not in new_locs]
        all_names = sorted(set(known.values()) | set(new_chars), key=len, reverse=True)
        all_locs = sorted(set(known_locs.values()) | set(new_locs), key=len, reverse=True)
        events, loc_events = [], []
        for s in _sentences(text):
            low = s.lower()
            loc = next((n for n in all_locs if n.lower() in low), None)
            if loc and re.search(r"sụp đổ|cháy rụi|tan hoang|đổ nát|phá hủy", low):
                loc_events.append({"location": loc, "field": "condition", "value": "ruined, burned debris",
                                   "permanent": True, "lasts_chapters": None, "evidence": s})
            if loc and re.search(r"treo đèn|kết hoa|trang hoàng", low):
                loc_events.append({"location": loc, "field": "decor", "value": "festival lanterns and flowers",
                                   "permanent": False, "lasts_chapters": 1, "evidence": s})
            who = next((n for n in all_names if n.lower() in low), None)
            if not who:
                continue
            m = re.search(r"(khoác|mặc|thay)\s+(?:lên\s+)?([^,.;!?]+)", s, re.I)
            if m:
                events.append({"character": who, "field": "outfit", "value": m.group(2).strip()[:60],
                               "permanent": False, "lasts_chapters": None, "evidence": s})
            if "sẹo" in low:
                side = "right cheek" if "phải" in low else ("left cheek" if "trái" in low else "face")
                events.append({"character": who, "field": "mark", "value": f"long scar on {side}",
                               "permanent": True, "lasts_chapters": None, "evidence": s})
            elif "bị thương" in low or "băng bó" in low:
                events.append({"character": who, "field": "injury", "value": "bandaged wound",
                               "permanent": False, "lasts_chapters": 2, "evidence": s})
            if "tóc" in low and re.search(r"cắt|nhuộm|búi", low):
                events.append({"character": who, "field": "hair", "value": "new hairstyle",
                               "permanent": False, "lasts_chapters": None, "evidence": s})
            m = re.search(r"(\d+)\s*tuổi", low)
            if m:
                events.append({"character": who, "field": "age", "value": f"{m.group(1)} years old",
                               "permanent": True, "lasts_chapters": None, "evidence": s})
        sents = _sentences(text)
        return {
            "summary": " ".join(sents[:2])[:400],
            "new_characters": [{"name": n, "aliases": [], "appearance": f"distinct look for {n}", "role": ""} for n in new_chars],
            "events": events,
            "locations": [{"name": n, "aliases": [], "description": f"scenic view of {n}"} for n in new_locs],
            "location_events": loc_events,
        }

    def _beats(self, meta: dict) -> dict:
        paras = meta.get("paragraphs", [])
        known = self._known(meta.get("known_characters", []))
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
            variant = "night" if re.search(r"đêm|tối|trăng", low) else ("dawn" if "sáng sớm" in low else "default")
            quotes = re.findall(r"[“\"]([^”\"]+)[”\"]", chunk)
            dialogue = [{"character": cast[0]["character"] if cast else "", "text": q, "kind": "speech"} for q in quotes[:3]]
            beats.append({"start": s + 1, "end": min(s + per, len(paras)), "location": cur_loc, "variant": variant,
                          "cast": cast[:3], "shot": shots[k % 3], "action": "characters in the scene",
                          "mood": "cinematic", "dialogue": dialogue})
        return {"beats": beats}

    def _dedupe(self, meta: dict) -> dict:
        pairs = []
        names = [c["name"] for c in meta.get("characters", [])]
        for a in names:
            for b in names:
                if a != b and b.split()[-1] == a.split()[-1] and len(b.split()) < len(a.split()):
                    pairs.append({"keep": a, "merge": b, "reason": "tên ngắn trùng phần cuối tên đầy đủ"})
        return {"characters": pairs, "locations": []}


class MockImage(Adapter):
    kind = "image.generate"
    type_name = "mock-image"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = ImageGeneratePayload(**job.payload)
        steps = int(self.cfg.get("steps", 3))
        for i in range(steps):
            ctx.check_cancel()
            ctx.report((i + 1) / steps, f"bước {i + 1}/{steps}")
            time.sleep(float(self.cfg.get("delay", 0)) / steps)
        w, h = max(64, p.width // 2), max(64, p.height // 2)
        hsh = hashlib.sha1(f"{p.prompt}{p.seed}".encode()).digest()
        img = Image.new("RGB", (w, h), (80 + hsh[0] % 120, 80 + hsh[1] % 120, 90 + hsh[2] % 120))
        d = ImageDraw.Draw(img)
        d.ellipse((w // 2 - w // 8, h // 3 - h // 8, w // 2 + w // 8, h // 3 + h // 8), outline=(255, 255, 255), width=3)
        for i, ref in enumerate(p.refs[:4]):
            try:
                th = Image.open(ctx.fetch_asset(ref.asset_id, ref.sha256)).convert("RGB")
                th.thumbnail((w // 5, h // 4))
                img.paste(th, (8 + i * (w // 5 + 4), h - th.height - 8))
            except Exception:  # noqa: BLE001
                pass
        y = 8
        for line in textwrap.wrap(p.prompt, width=max(20, w // 7))[:10]:
            d.text((8, y), line, fill=(255, 255, 255))
            y += 13
        d.text((w - 80, 8), f"seed {p.seed % 10000}", fill=(255, 255, 0))
        out = ctx.workdir / f"img_{job.id}.png"
        img.save(out, "PNG")
        return AdapterResult(output={"files": [out.name], "seed": p.seed}, files=[out], model_id=self.model)


class MockTTS(Adapter):
    kind = "tts.synthesize"
    type_name = "mock-tts"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = TtsPayload(**job.payload)
        words = re.findall(r"\S+", p.text)
        per_word = float(self.cfg.get("seconds_per_word", 0.28)) / max(0.25, p.rate)
        dur = max(1.0, len(words) * per_word)
        rate = 16000
        out = ctx.workdir / f"tts_{job.id}.wav"
        with wave.open(str(out), "wb") as wv:
            wv.setnchannels(1)
            wv.setsampwidth(2)
            wv.setframerate(rate)
            wv.writeframes(struct.pack("<h", 0) * int(dur * rate))
        t, wt = 0.0, []
        for w in words:
            wt.append({"start": round(t, 3), "end": round(t + per_word, 3), "text": w})
            t += per_word
        return AdapterResult(output={"file": out.name, "duration": dur, "words": wt, "sentences": []}, files=[out],
                             model_id=self.model)


class MockRemoveBg(Adapter):
    kind = "image.remove_bg"
    type_name = "mock-rembg"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = RemoveBgPayload(**job.payload)
        out = ctx.workdir / f"nobg_{job.id}.png"
        shutil.copyfile(ctx.fetch_asset(p.image.asset_id, p.image.sha256), out)
        return AdapterResult(output={"files": [out.name]}, files=[out], model_id=self.model)
