"""Adapter chay lenh dong lenh bat ky theo mau.

Dung cho mflux, script ComfyUI, script TTS rieng... Khong phu thuoc phien ban
thu vien: doi cong cu chi can doi mau lenh trong worker.toml.

Placeholder: {prompt} {negative} {width} {height} {seed} {steps} {output} {model}
             {text} {text_file} {voice} {rate} {input}
Token dac biet "{refs}" duoc thay bang danh sach duong dan anh tham chieu.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from storyforge.protocol import ClaimedJob, ImageGeneratePayload, RemoveBgPayload, TtsPayload

from .base import Adapter, AdapterResult, JobContext, RetryableError, probe_duration


def _render(tokens: list[str] | str, values: dict, refs: list[str], ref_flag: str = "") -> list[str]:
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
        out.append(t.format(**values))
    return out


def _run(cmd: list[str], timeout: float, ctx: JobContext) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ctx.workdir)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise RetryableError(f"Lệnh quá thời gian {timeout}s") from e
    if proc.returncode != 0:
        raise RuntimeError(f"Lệnh lỗi {proc.returncode}: {' '.join(cmd)[:300]}\n{(out or '')[-2000:]}")


def _find_output(path: Path) -> Path:
    if path.exists():
        return path
    cands = sorted(path.parent.glob(path.stem + "*"), key=lambda p: p.stat().st_mtime, reverse=True)
    cands = [c for c in cands if c.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".m4a")]
    if not cands:
        raise RuntimeError(f"Không thấy file kết quả {path.name}")
    return cands[0]


class CommandImage(Adapter):
    """Tao anh bang lenh. cmd dung khi khong co anh tham chieu, cmd_with_refs khi co."""
    kind = "image.generate"
    type_name = "command"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = ImageGeneratePayload(**job.payload)
        out = ctx.workdir / f"img_{job.id}.png"
        refs = [str(ctx.fetch_asset(r.asset_id, r.sha256)) for r in p.refs]
        max_refs = int(self.cfg.get("max_refs", 4))
        refs = refs[:max_refs]
        steps = p.steps or self.cfg.get("default_steps", 8)
        vals = {"prompt": p.prompt, "negative": p.negative_prompt, "width": p.width, "height": p.height,
                "seed": p.seed, "steps": steps, "output": str(out), "model": self.model}
        tpl = self.cfg.get("cmd_with_refs") if refs and self.cfg.get("cmd_with_refs") else self.cfg["cmd"]
        if refs and not self.cfg.get("cmd_with_refs"):
            refs = []  # cong cu khong ho tro anh tham chieu
        cmd = _render(tpl, vals, refs, str(self.cfg.get("ref_flag", "")))
        _run(cmd, float(self.cfg.get("timeout", 1800)), ctx)
        f = _find_output(out)
        return AdapterResult(output={"files": [f.name], "seed": p.seed}, files=[f], model_id=self.model)


class CommandTTS(Adapter):
    kind = "tts.synthesize"
    type_name = "command"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = TtsPayload(**job.payload)
        ext = str(self.cfg.get("ext", ".wav"))
        out = ctx.workdir / f"tts_{job.id}{ext}"
        tf = ctx.workdir / f"tts_{job.id}.txt"
        tf.write_text(p.text, encoding="utf-8")
        vals = {"text": p.text, "text_file": str(tf), "voice": p.voice or self.cfg.get("voice", ""),
                "rate": p.rate, "output": str(out), "model": self.model}
        _run(_render(self.cfg["cmd"], vals, []), float(self.cfg.get("timeout", 600)), ctx)
        f = _find_output(out)
        dur = probe_duration(f, str(self.cfg.get("ffprobe", "ffprobe"))) or 0.0
        return AdapterResult(output={"file": f.name, "duration": dur, "sentences": []}, files=[f], model_id=self.model)


class CommandRemoveBg(Adapter):
    kind = "image.remove_bg"
    type_name = "command"

    def run(self, job: ClaimedJob, ctx: JobContext) -> AdapterResult:
        p = RemoveBgPayload(**job.payload)
        src = ctx.fetch_asset(p.image.asset_id, p.image.sha256)
        out = ctx.workdir / f"nobg_{job.id}.png"
        _run(_render(self.cfg["cmd"], {"input": str(src), "output": str(out), "model": self.model}, []),
             float(self.cfg.get("timeout", 600)), ctx)
        f = _find_output(out)
        return AdapterResult(output={"files": [f.name]}, files=[f], model_id=self.model)
