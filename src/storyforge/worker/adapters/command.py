"""Adapter chạy lệnh dòng lệnh bất kỳ theo mẫu (mflux, script ComfyUI, script TTS...).

Placeholder: {prompt} {negative} {width} {height} {seed} {steps} {output} {model}
             {text} {text_file} {voice} {rate} {input}
Token "{refs}": danh sách ảnh tham chiếu (có ref_flag phía trước nếu cấu hình); "{refs_repeat}": lặp "ref_flag ảnh".
Dấu {...} khác được giữ nguyên. Tiến độ đọc từ log dạng "37%" hoặc "12/30". Bấm Hủy sẽ dừng tiến trình.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from storyforge.protocol import ClaimedJob, ImageGeneratePayload, RemoveBgPayload, TtsPayload

from .base import Adapter, AdapterResult, Cancelled, JobContext, RetryableError, probe_duration

_PCT = re.compile(r"(\d{1,3})%")
_FRAC = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")


class _Keep(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fmt(t: str, values: dict) -> str:
    try:
        return t.format_map(_Keep(values))
    except (ValueError, IndexError, AttributeError):
        out = t
        for k, v in values.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def render_cmd(tokens: list[str] | str, values: dict, refs: list[str], ref_flag: str = "") -> list[str]:
    if isinstance(tokens, str):
        tokens = shlex.split(tokens)
    out: list[str] = []
    for t in tokens:
        if t == "{refs}":
            if refs:
                if ref_flag:
                    out.append(ref_flag)
                out.extend(refs)
            continue
        if t == "{refs_repeat}":
            for r in refs:
                out += [ref_flag or "--image", r]
            continue
        out.append(_fmt(t, values))
    return out


def run_cmd(cmd: list[str], timeout: float, ctx: JobContext) -> str:
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ctx.workdir,
                                bufsize=1, errors="replace")
    except FileNotFoundError as e:
        raise RuntimeError(f"Không tìm thấy chương trình '{cmd[0]}'. Kiểm tra đã cài và có trong PATH, "
                           f"hoặc sửa mẫu lệnh trong worker.toml.") from e
    lines: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        buf = ""
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                if buf:
                    lines.append(buf)
                    m = _PCT.search(buf)
                    if m and int(m.group(1)) <= 100:
                        ctx.report(int(m.group(1)) / 100, buf[-80:])
                    else:
                        f = _FRAC.search(buf)
                        if f and 0 < int(f.group(2)) <= 10000 and int(f.group(1)) <= int(f.group(2)):
                            ctx.report(int(f.group(1)) / int(f.group(2)), buf[-80:])
                buf = ""
            else:
                buf += ch
        if buf:
            lines.append(buf)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    start = time.time()
    while proc.poll() is None:
        if ctx.is_cancelled():
            proc.kill()
            raise Cancelled("Job đã bị hủy")
        if time.time() - start > timeout:
            proc.kill()
            raise RetryableError(f"Lệnh quá thời gian {timeout:.0f}s")
        time.sleep(0.2)
    t.join(timeout=2)
    out = "\n".join(lines)
    if proc.returncode != 0:
        raise RuntimeError(f"Lệnh lỗi {proc.returncode}: {' '.join(cmd)[:300]}\n{out[-2000:]}")
    return out


def find_output(path: Path) -> Path:
    if path.exists():
        return path
    cands = sorted(path.parent.glob(path.stem + "*"), key=lambda p: p.stat().st_mtime, reverse=True)
    cands = [c for c in cands if c.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".m4a")]
    if not cands:
        raise RuntimeError(f"Không thấy file kết quả {path.name}")
    return cands[0]


class CommandImage(Adapter):
    kind = "image.generate"
    type_name = "command"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = ImageGeneratePayload(**job.payload)
        out = ctx.workdir / f"img_{job.id}.png"
        refs = [str(ctx.fetch_asset(r.asset_id, r.sha256)) for r in p.refs][: int(self.cfg.get("max_refs", 4))]
        vals = {"prompt": p.prompt, "negative": p.negative_prompt, "width": p.width, "height": p.height,
                "seed": p.seed, "steps": p.steps or self.cfg.get("default_steps", 8), "output": str(out), "model": self.model}
        tpl = self.cfg.get("cmd_with_refs") if refs and self.cfg.get("cmd_with_refs") else self.cfg["cmd"]
        if refs and not self.cfg.get("cmd_with_refs"):
            refs = []
        run_cmd(render_cmd(tpl, vals, refs, str(self.cfg.get("ref_flag", ""))), float(self.cfg.get("timeout", 1800)), ctx)
        f = find_output(out)
        return AdapterResult(output={"files": [f.name], "seed": p.seed}, files=[f], model_id=self.model)


class CommandTTS(Adapter):
    kind = "tts.synthesize"
    type_name = "command"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = TtsPayload(**job.payload)
        out = ctx.workdir / f"tts_{job.id}{self.cfg.get('ext', '.wav')}"
        tf = ctx.workdir / f"tts_{job.id}.txt"
        tf.write_text(p.text, encoding="utf-8")
        vals = {"text": p.text, "text_file": str(tf), "voice": p.voice or self.cfg.get("voice", ""),
                "rate": p.rate, "output": str(out), "model": self.model}
        run_cmd(render_cmd(self.cfg["cmd"], vals, []), float(self.cfg.get("timeout", 600)), ctx)
        f = find_output(out)
        return AdapterResult(output={"file": f.name, "duration": probe_duration(f, str(self.cfg.get("ffprobe", "ffprobe"))) or 0.0},
                             files=[f], model_id=self.model)


class CommandRemoveBg(Adapter):
    kind = "image.remove_bg"
    type_name = "command"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = RemoveBgPayload(**job.payload)
        src = ctx.fetch_asset(p.image.asset_id, p.image.sha256)
        out = ctx.workdir / f"nobg_{job.id}.png"
        run_cmd(render_cmd(self.cfg["cmd"], {"input": str(src), "output": str(out), "model": self.model}, []),
                float(self.cfg.get("timeout", 600)), ctx)
        f = find_output(out)
        return AdapterResult(output={"files": [f.name]}, files=[f], model_id=self.model)
