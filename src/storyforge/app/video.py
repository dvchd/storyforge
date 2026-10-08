"""Dung video tu anh tinh va audio bang ffmpeg.

Moi segment: anh + hieu ung zoom cham (Ken Burns) + audio. Sau do noi cac doan
bang concat, khong ma hoa lai. Phu de mem xuat rieng dang SRT.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def pick_encoder(setting: str) -> list[str]:
    if setting and setting != "auto":
        enc = setting
    else:
        enc = "h264_videotoolbox" if sys.platform == "darwin" else "libx264"
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return ["-c:v", enc, "-b:v", "6M"]


def estimate_duration(text: str) -> float:
    return max(3.0, len(text.strip()) / 14.0)


def fmt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(items: list[dict]) -> str:
    """items: {start, end, text}"""
    out = []
    for i, it in enumerate(items, 1):
        out.append(f"{i}\n{fmt_ts(it['start'])} --> {fmt_ts(it['end'])}\n{it['text'].strip()}\n")
    return "\n".join(out)


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg loi ({r.returncode}): {r.stderr[-2000:]}")


def render(segments: list[dict], out_dir: Path, ffmpeg: str, width: int, height: int, fps: int = 30,
           encoder: str = "auto", zoom: float = 0.10) -> Path:
    """segments: {image: Path|None, audio: Path|None, duration: float}"""
    out_dir.mkdir(parents=True, exist_ok=True)
    enc = pick_encoder(encoder)
    parts: list[Path] = []
    for i, s in enumerate(segments):
        dur = max(0.5, float(s["duration"]))
        frames = max(1, int(round(dur * fps)))
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
        z = f"1+{zoom}*on/{frames}"
        vf = (f"[0:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
              f"crop={width * 2}:{height * 2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps},"
              f"format=yuv420p[v]")
        cmd += ["-filter_complex", vf, "-map", "[v]", "-map", "1:a", *enc,
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                "-t", f"{dur:.3f}", "-frames:v", str(frames), str(part)]
        _run(cmd)
        parts.append(part)
    lst = out_dir / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    final = out_dir / "chapter.mp4"
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
          "-c", "copy", "-movflags", "+faststart", str(final)])
    return final
