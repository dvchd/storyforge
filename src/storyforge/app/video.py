"""Dựng video từ ảnh tĩnh và audio bằng ffmpeg.

Mỗi đoạn: cắt ảnh gốc theo điểm lấy nét (focus) -> chuyển động (zoom vào/ra, lia trái/phải)
-> mờ dần đầu cuối -> audio chuẩn hóa âm lượng (loudnorm) + khoảng lặng giữa đoạn.
Sau đó nối các đoạn bằng concat (không mã hóa lại). Phụ đề mềm xuất riêng dạng SRT.
"""
from __future__ import annotations

import math
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from .checks import syllables

MOTIONS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "none"]
MOTION_LABELS = {"auto": "Tự động", "zoom_in": "Zoom vào", "zoom_out": "Zoom ra", "pan_left": "Lia sang trái",
                 "pan_right": "Lia sang phải", "none": "Đứng yên"}


def pick_encoder(setting: str) -> list[str]:
    enc = setting if setting and setting != "auto" else ("h264_videotoolbox" if sys.platform == "darwin" else "libx264")
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return ["-c:v", enc, "-b:v", "6M"]


def auto_motion(idx: int, shot: str) -> str:
    if shot == "wide":
        return "pan_right" if idx % 2 == 0 else "pan_left"
    if shot == "close":
        return "zoom_in"
    return "zoom_in" if idx % 2 == 0 else "zoom_out"


def estimate_duration(text: str) -> float:
    n_sent = max(1, len(re.findall(r"[.!?…]+", text or "")))
    return max(2.5, syllables(text) * 0.27 + 0.35 * n_sent)


def probe_duration(path: Path | None, ffprobe: str) -> float | None:
    if not path or not Path(path).exists():
        return None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------ SRT
def fmt_ts(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap(text: str, width: int = 42) -> str:
    return "\n".join(textwrap.wrap(text.strip(), width=width)[:2]) or text.strip()


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts = [p.strip() for p in re.split(r"(?<=[,;:])\s+", text) if p.strip()]
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + 1 + len(p) > max_chars:
            out.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        out.append(cur)
    final = []
    for o in out:
        while len(o) > max_chars:
            cut = o.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            final.append(o[:cut].strip())
            o = o[cut:].strip()
        if o:
            final.append(o)
    return final


def cues_for_segment(narration: str, duration: float, timings: dict, offset: float, max_chars: int = 84) -> list[dict]:
    """Ưu tiên mốc từng từ, rồi mốc câu, cuối cùng chia theo số âm tiết."""
    words = timings.get("words") or []
    if words:
        cues, cur, start = [], [], None
        for w in words:
            if start is None:
                start = w["start"]
            cur.append(w["text"])
            line = " ".join(cur)
            if len(line) >= max_chars - 10 or re.search(r"[.!?…]$", w["text"]) or w["end"] - start > 5.5:
                cues.append({"start": offset + start, "end": offset + w["end"], "text": line})
                cur, start = [], None
        if cur:
            cues.append({"start": offset + start, "end": offset + words[-1]["end"], "text": " ".join(cur)})
        return cues
    sents = timings.get("sentences") or []
    if sents:
        cues = []
        for s in sents:
            pieces = _split_long(s["text"], max_chars)
            total = sum(syllables(p) for p in pieces)
            t = s["start"]
            for p in pieces:
                d = (s["end"] - s["start"]) * syllables(p) / total
                cues.append({"start": offset + t, "end": offset + t + d, "text": p})
                t += d
        return cues
    pieces = []
    for s in re.split(r"(?<=[.!?…])\s+", narration.replace("\n", " ")):
        if s.strip():
            pieces += _split_long(s.strip(), max_chars)
    total = sum(syllables(p) for p in pieces) or 1
    cues, t = [], 0.0
    for p in pieces:
        d = duration * syllables(p) / total
        cues.append({"start": offset + t, "end": offset + t + d, "text": p})
        t += d
    return cues


def build_srt(cues: list[dict]) -> str:
    return "\n".join(f"{i}\n{fmt_ts(c['start'])} --> {fmt_ts(c['end'])}\n{_wrap(c['text'])}\n"
                     for i, c in enumerate(cues, 1))


# ------------------------------------------------------------------ render
def _zoompan(motion: str, z: float, frames: int, w: int, h: int, fps: int) -> str:
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "zoom_out":
        zz, x, y = f"1+{z}-{z}*on/{frames}", cx, cy
    elif motion == "pan_right":
        zz, x, y = f"1+{z}", f"(iw-iw/zoom)*on/{frames}", cy
    elif motion == "pan_left":
        zz, x, y = f"1+{z}", f"(iw-iw/zoom)*(1-on/{frames})", cy
    elif motion == "none":
        zz, x, y = "1", "0", "0"
    else:
        zz, x, y = f"1+{z}*on/{frames}", cx, cy
    return f"zoompan=z='{zz}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps}"


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg lỗi ({r.returncode}): {r.stderr[-2000:]}")


def render(segments: list[dict], out_dir: Path, ffmpeg: str, ffprobe: str, width: int, height: int, fps: int = 30,
           encoder: str = "auto", zoom: float = 0.10, transition: float = 0.3, gap: float = 0.35,
           loudnorm: bool = True, progress=None) -> tuple[Path, list[float]]:
    """segments: {image, focus_x, focus_y, audio, duration, motion, shot}. Trả (file mp4, thời lượng từng đoạn)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    enc = pick_encoder(encoder)
    parts: list[Path] = []
    durs: list[float] = []
    for i, s in enumerate(segments):
        adur = probe_duration(s.get("audio"), ffprobe) if s.get("audio") else None
        base = adur if adur else float(s.get("duration") or 3.0)
        dur = max(1.0, base + gap)
        durs.append(dur)
        frames = max(1, int(round(dur * fps)))
        motion = s.get("motion") or "auto"
        if motion == "auto":
            motion = auto_motion(i, s.get("shot") or "medium")
        part = out_dir / f"seg_{i:04d}.mp4"
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if s.get("image") and Path(s["image"]).exists():
            cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(s["image"])]
        else:
            cmd += ["-f", "lavfi", "-i", f"color=c=0x202028:s={width}x{height}:r={fps}"]
        if s.get("audio") and Path(s["audio"]).exists():
            cmd += ["-i", str(s["audio"])]
        else:
            cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
        fx, fy = float(s.get("focus_x", .5)), float(s.get("focus_y", .5))
        W2, H2 = width * 2, height * 2
        vf = (f"[0:v]scale={W2}:{H2}:force_original_aspect_ratio=increase,"
              f"crop={W2}:{H2}:x='(iw-ow)*{fx:.3f}':y='(ih-oh)*{fy:.3f}',"
              + _zoompan(motion, zoom, frames, width, height, fps))
        if transition > 0 and dur > 2 * transition:
            vf += f",fade=t=in:st=0:d={transition},fade=t=out:st={dur - transition:.3f}:d={transition}"
        vf += ",format=yuv420p[v]"
        af = "[1:a]aresample=44100"
        if loudnorm and s.get("audio"):
            af += ",loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100"
        af += f",apad,atrim=0:{dur:.3f},afade=t=in:d=0.04,afade=t=out:st={max(0, dur - 0.2):.3f}:d=0.2[a]"
        cmd += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]", *enc,
                "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-t", f"{dur:.3f}", "-frames:v", str(frames), str(part)]
        _run(cmd)
        parts.append(part)
        if progress:
            progress((i + 1) / (len(segments) + 1), f"đoạn {i + 1}/{len(segments)}")
    lst = out_dir / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    final = out_dir / "chapter.mp4"
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
          "-c", "copy", "-movflags", "+faststart", str(final)])
    return final, durs


math  # giữ import
